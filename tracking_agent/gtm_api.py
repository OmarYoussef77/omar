"""Push a generated container into a GTM workspace via the Tag Manager API v2.

Nothing here publishes on its own: push_to_workspace() writes to a
workspace (a draft), create_version() snapshots it, and publish_version()
makes it live. The agent asks for approval before each of those steps.

Auth: a service account JSON (gtm.credentials_file; add the service
account's email as a user on the GTM container with Publish access), or
Application Default Credentials (`gcloud auth application-default login
--scopes=https://www.googleapis.com/auth/tagmanager.edit.containers,...`).
"""

from __future__ import annotations

from typing import Any

SCOPES = [
    "https://www.googleapis.com/auth/tagmanager.edit.containers",
    "https://www.googleapis.com/auth/tagmanager.edit.containerversions",
    "https://www.googleapis.com/auth/tagmanager.publish",
]

_EXPORT_ONLY_FIELDS = {"accountId", "containerId", "workspaceId", "path", "fingerprint", "tagManagerUrl"}


def _clean(entity: dict[str, Any], id_field: str) -> dict[str, Any]:
    return {k: val for k, val in entity.items() if k not in _EXPORT_ONLY_FIELDS and k != id_field}


def plan_push(export: dict[str, Any], existing: dict[str, list[dict[str, Any]]]) -> dict[str, list[str]]:
    """Diff the export against a workspace's current entities, matched by name."""
    cv = export["containerVersion"]
    plan: dict[str, list[str]] = {"create": [], "update": []}
    for kind, items in (("variable", cv["variable"]), ("trigger", cv["trigger"]), ("tag", cv["tag"])):
        existing_names = {e["name"] for e in existing.get(kind, [])}
        for item in items:
            action = "update" if item["name"] in existing_names else "create"
            plan[action].append(f"{kind}: {item['name']}")
    return plan


class GTMClient:
    def __init__(self, account_id: str, container_id: str, credentials_file: str = ""):
        try:
            import google.auth
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise RuntimeError(
                "GTM API support needs the optional dependencies: pip install 'tracking-agent[gtm]'"
            ) from exc

        if credentials_file:
            creds = service_account.Credentials.from_service_account_file(credentials_file, scopes=SCOPES)
        else:
            creds, _ = google.auth.default(scopes=SCOPES)
        self.service = build("tagmanager", "v2", credentials=creds, cache_discovery=False)
        self.container_path = f"accounts/{account_id}/containers/{container_id}"

    # -- workspaces ----------------------------------------------------------
    def _workspaces(self):
        return self.service.accounts().containers().workspaces()

    def get_or_create_workspace(self, name: str) -> str:
        listing = self._workspaces().list(parent=self.container_path).execute()
        for ws in listing.get("workspace", []):
            if ws["name"] == name:
                return ws["path"]
        created = self._workspaces().create(
            parent=self.container_path,
            body={"name": name, "description": "Created by tracking-agent"},
        ).execute()
        return created["path"]

    def list_entities(self, workspace_path: str) -> dict[str, list[dict[str, Any]]]:
        ws = self._workspaces()
        return {
            "variable": ws.variables().list(parent=workspace_path).execute().get("variable", []),
            "trigger": ws.triggers().list(parent=workspace_path).execute().get("trigger", []),
            "tag": ws.tags().list(parent=workspace_path).execute().get("tag", []),
        }

    # -- push ----------------------------------------------------------------
    def push_to_workspace(self, export: dict[str, Any], workspace_path: str) -> dict[str, Any]:
        """Upsert variables, triggers, then tags (remapping trigger IDs)."""
        cv = export["containerVersion"]
        ws = self._workspaces()
        existing = self.list_entities(workspace_path)
        by_name = {kind: {e["name"]: e for e in items} for kind, items in existing.items()}
        results: dict[str, Any] = {"created": [], "updated": []}

        def upsert(kind: str, resource, entity: dict[str, Any], id_field: str) -> dict[str, Any]:
            body = _clean(entity, id_field)
            current = by_name[kind].get(entity["name"])
            if current:
                saved = resource.update(path=current["path"], body=body).execute()
                results["updated"].append(f"{kind}: {entity['name']}")
            else:
                saved = resource.create(parent=workspace_path, body=body).execute()
                results["created"].append(f"{kind}: {entity['name']}")
            return saved

        for variable in cv["variable"]:
            upsert("variable", ws.variables(), variable, "variableId")

        trigger_id_map: dict[str, str] = {}
        for trigger in cv["trigger"]:
            saved = upsert("trigger", ws.triggers(), trigger, "triggerId")
            trigger_id_map[trigger["triggerId"]] = saved["triggerId"]

        # Setup tags must exist before the tags that reference them by name.
        tags = sorted(cv["tag"], key=lambda t: 0 if "setupTag" not in t else 1)
        for tag in tags:
            tag = dict(tag)
            tag["firingTriggerId"] = [trigger_id_map[t] for t in tag.get("firingTriggerId", [])]
            upsert("tag", ws.tags(), tag, "tagId")

        results["workspace"] = workspace_path
        return results

    def create_version(self, workspace_path: str, name: str, notes: str = "") -> dict[str, Any]:
        response = self._workspaces().create_version(
            path=workspace_path, body={"name": name, "notes": notes}
        ).execute()
        if response.get("compilerError"):
            raise RuntimeError(f"GTM reported a compiler error: {response}")
        version = response["containerVersion"]
        return {"path": version["path"], "containerVersionId": version["containerVersionId"]}

    def publish_version(self, version_path: str) -> dict[str, Any]:
        response = self.service.accounts().containers().versions().publish(path=version_path).execute()
        if response.get("compilerError"):
            raise RuntimeError(f"GTM reported a compiler error: {response}")
        return {"published": response["containerVersion"]["containerVersionId"]}
