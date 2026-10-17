"""资源管理器文件夹右键「用媒体播放器打开」的注册/注销（winreg 用假实现，不碰真注册表）。"""
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app import assoc  # noqa: E402


class _Key:
    def __init__(self, store, path):
        self.store, self.path = store, path

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_winreg(store):
    w = types.SimpleNamespace(HKEY_CURRENT_USER=1, REG_SZ=1, REG_NONE=0,
                              KEY_SET_VALUE=2, KEY_ALL_ACCESS=3)

    def create(_root, path):
        # 与真 winreg 一致：CreateKey 会建出所有中间层键
        parts = path.split("\\")
        for i in range(1, len(parts) + 1):
            store.setdefault("\\".join(parts[:i]), {})
        return _Key(store, path)

    def open_(_root, path, *_a):
        if path not in store:
            raise OSError(path)
        return _Key(store, path)

    def set_value(key, name, _r, _t, value):
        store[key.path][name] = value

    def enum_key(key, i):
        subs = sorted({p[len(key.path) + 1:].split("\\")[0] for p in store
                       if p.startswith(key.path + "\\")})
        if i >= len(subs):
            raise OSError
        return subs[i]

    def delete_key(_root, path):
        store.pop(path, None)

    def delete_value(key, name):
        store[key.path].pop(name, None)

    w.CreateKey, w.OpenKey, w.SetValueEx = create, open_, set_value
    w.EnumKey, w.DeleteKey, w.DeleteValue = enum_key, delete_key, delete_value
    w.CloseKey = lambda k: None
    return w


class FolderMenuTests(unittest.TestCase):
    def _run(self, fn, store):
        with mock.patch.dict(sys.modules, {"winreg": _fake_winreg(store)}), \
                mock.patch.object(assoc, "is_supported", return_value=True), \
                mock.patch.object(assoc, "_notify_shell"):
            fn()

    def test_register_adds_background_folder_and_drive_menus(self):
        store = {}
        self._run(assoc.register, store)
        for base in (r"Directory\Background", "Directory", "Drive"):
            cmd_key = rf"Software\Classes\{base}\shell\{assoc.FOLDER_VERB}\command"
            self.assertIn(cmd_key, store)
            cmd = store[cmd_key][""]
            # 末尾 \. 防盘符根 "I:\" 把收尾引号转义掉
            self.assertTrue(cmd.endswith(r'"%V\."'), cmd)

    def test_unregister_removes_folder_menus(self):
        store = {}
        self._run(assoc.register, store)
        self._run(assoc.unregister, store)
        self.assertFalse([p for p in store if assoc.FOLDER_VERB in p])
        self.assertFalse([p for p in store if assoc.PROGID in p and "OpenWithProgids" not in p])

    def test_trailing_dot_resolves_to_folder(self):
        self.assertEqual(Path(r"I:\."), Path("I:/"))
        self.assertEqual(Path(r"I:\a\b\."), Path(r"I:\a\b"))


if __name__ == "__main__":
    unittest.main()
