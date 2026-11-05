import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _astrbot_stubs  # noqa: E402

_astrbot_stubs.install()
from onebot import OneBotMixin  # noqa: E402


class _Storage:
    def __init__(self, supers=()):
        self.supers = set(supers)

    def is_group_admin_blocked(self, gid, uid):
        return False

    def is_group_super_admin(self, gid, uid):
        return (gid, uid) in self.supers

    def get_group_admin_grant(self, gid):
        return None


class _Harness(OneBotMixin):
    def __init__(self, roles, cfg=None, admins=(), supers=()):
        self.roles = roles
        self.cfg = cfg or {}
        self.admins = set(admins)
        self._storage = _Storage(supers)
        self._group_white_set = set()
        self._group_black_set = set()

    def _try_get_sender_id(self, event):
        return event["uid"]

    def _get_group_id(self, event):
        return event["gid"]

    def _get_all_admin_ids(self):
        return self.admins

    def _cached_perm(self, kind, key, loader):
        return loader()

    async def _get_member_role(self, event, gid, uid):
        return self.roles.get(uid, "member")

    def _cfg(self, key, default=True, group_id=None):
        return self.cfg.get(key, default)


def _ev(uid, gid="100"):
    return {"uid": uid, "gid": gid}


ROLES = {"o": "owner", "a": "admin", "m": "member"}


class StrictPermissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_owner_allowed_admin_denied(self):
        h = _Harness(ROLES)
        self.assertTrue(await h._is_admin(_ev("o")))
        self.assertFalse(await h._is_admin(_ev("a")))
        self.assertFalse(await h._is_admin(_ev("m")))

    async def test_manual_admins_allowed(self):
        h = _Harness(ROLES, admins={"a"}, supers={("100", "m")})
        self.assertTrue(await h._is_admin(_ev("a")))
        self.assertTrue(await h._is_admin(_ev("m")))
        self.assertFalse(await h._is_admin(_ev("m", gid="200")))

    async def test_strict_off_restores_group_admin(self):
        h = _Harness(ROLES, cfg={"strict_permission_enabled": False})
        self.assertTrue(await h._is_admin(_ev("a")))
        self.assertTrue(await h._is_admin(_ev("o")))

    async def test_legacy_off_denies_owner_too(self):
        h = _Harness(ROLES, cfg={"legacy_role_admin_enabled": False})
        self.assertFalse(await h._is_admin(_ev("o")))


if __name__ == "__main__":
    unittest.main()
