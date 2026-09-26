"""push_to_workspace against an in-memory fake of the Tag Manager v2 service."""

import itertools

from tracking_agent import gtm_builder
from tracking_agent.gtm_api import GTMClient


class _Call:
    def __init__(self, result):
        self.result = result

    def execute(self):
        return self.result


class _Collection:
    def __init__(self, kind, store, counter):
        self.kind, self.store, self.counter = kind, store, counter

    def list(self, parent):
        return _Call({self.kind: list(self.store.values())})

    def create(self, parent, body):
        new_id = str(next(self.counter))
        saved = {**body, f"{self.kind}Id": new_id, "path": f"{parent}/{self.kind}s/{new_id}"}
        self.store[body["name"]] = saved
        return _Call(saved)

    def update(self, path, body):
        current = next(e for e in self.store.values() if e["path"] == path)
        saved = {**current, **body}
        self.store[body["name"]] = saved
        return _Call(saved)


class _Workspaces:
    def __init__(self):
        counter = itertools.count(500)
        self.stores = {"variable": {}, "trigger": {}, "tag": {}}
        self._collections = {k: _Collection(k, s, counter) for k, s in self.stores.items()}

    def variables(self):
        return self._collections["variable"]

    def triggers(self):
        return self._collections["trigger"]

    def tags(self):
        return self._collections["tag"]


def _client(workspaces):
    client = GTMClient.__new__(GTMClient)
    client.container_path = "accounts/1/containers/2"
    client._workspaces = lambda: workspaces
    return client


def test_push_remaps_trigger_ids_and_upserts(config):
    export = gtm_builder.build_container(config)
    workspaces = _Workspaces()
    client = _client(workspaces)
    ws = "accounts/1/containers/2/workspaces/3"

    first = client.push_to_workspace(export, ws)
    cv = export["containerVersion"]
    assert len(first["created"]) == len(cv["variable"]) + len(cv["trigger"]) + len(cv["tag"])

    triggers = workspaces.stores["trigger"]
    purchase_tag = workspaces.stores["tag"]["Meta - Purchase"]
    assert purchase_tag["firingTriggerId"] == [triggers["CE - purchase"]["triggerId"]]
    assert "accountId" not in purchase_tag

    # Setup (base) tags are created before the tags that reference them.
    order = list(workspaces.stores["tag"])
    assert order.index("Meta - Base (init)") < order.index("Meta - Purchase")

    second = client.push_to_workspace(export, ws)
    assert second["created"] == []
    assert len(second["updated"]) == len(first["created"])
