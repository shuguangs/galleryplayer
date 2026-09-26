"""网盘挂载成本地盘符（RaiDrive / CloudDrive / rclone 等）必须识别为远程。

背景（用户实测）：Y: 由 RaiDrive/CloudDrive 挂载，GetDriveType 报 3（本地固定
盘）、文件系统报 NTFS，旧 is_remote() 据此判"本地"——网络盘专属的保护
（缩略图串行、播放大缓冲、不做整夹预热）全部失效，每张视频缩略图实耗
80~100MB 流量，687 个视频的文件夹后台预热一遍约 60GB。

识别依据（实机对比 10 个盘符）：真实磁盘（NVMe/SATA/USB）都能用
IOCTL_STORAGE_QUERY_PROPERTY 查到物理总线；虚拟文件系统挂载没有底层
存储设备，查询失败；其设备名也是 \\Device\\Volume{GUID} 而非
\\Device\\HarddiskVolumeN。
"""
import unittest
from unittest import mock

from app import netpath


class _Probe:
    """替换系统调用：按盘符给出 GetDriveType / QueryDosDevice / 总线查询结果。"""

    def __init__(self, drives):
        self.drives = drives   # letter -> (drive_type, dos_device, has_bus)

    def drive_type(self, root):
        return self.drives[root[0]][0]

    def dos_device(self, letter):
        return self.drives[letter][1]

    def has_storage_bus(self, letter):
        return self.drives[letter][2]


REAL = {
    "C": (3, r"\Device\HarddiskVolume3", True),      # NVMe
    "I": (3, r"\Device\HarddiskVolume7", True),      # SATA
    "K": (3, r"\Device\HarddiskVolume14", True),     # USB 移动硬盘
    "Y": (3, r"\Device\Volume{66c6746d-b950-11f1-9ac7-047c1649d1c9}", False),  # RaiDrive
    "Z": (4, r"\Device\LanmanRedirector\;Z:0000\nas\share", False),           # SMB 映射
}


class VirtualMountTests(unittest.TestCase):
    def setUp(self):
        netpath._cache.clear()
        p = _Probe(REAL)
        self._patches = [
            mock.patch.object(netpath, "_drive_type", p.drive_type),
            mock.patch.object(netpath, "_dos_device", p.dos_device),
            mock.patch.object(netpath, "_has_storage_bus", p.has_storage_bus),
        ]
        for x in self._patches:
            x.start()

    def tearDown(self):
        for x in self._patches:
            x.stop()
        netpath._cache.clear()

    def test_virtual_mount_is_remote(self):
        self.assertTrue(netpath.is_remote(r"Y:\移动硬盘备份\x.mp4"))

    def test_real_disks_stay_local(self):
        for d in "CIK":
            with self.subTest(drive=d):
                self.assertFalse(netpath.is_remote(fr"{d}:\media\x.mp4"))

    def test_smb_mapped_and_unc_still_remote(self):
        self.assertTrue(netpath.is_remote(r"Z:\x.mp4"))
        self.assertTrue(netpath.is_remote(r"\\nas\share\x.mp4"))

    def test_probe_failure_defaults_to_local(self):
        """探测本身出错（权限等）时按本地处理：宁可少保护，不能把本地盘降速。"""
        with mock.patch.object(netpath, "_dos_device", side_effect=OSError):
            netpath._cache.clear()
            self.assertFalse(netpath.is_remote(r"C:\x.mp4"))


class RealMachineSmokeTests(unittest.TestCase):
    """真机冒烟：系统盘必须判本地（CI/任何 Windows 机都成立）。"""

    def test_system_drive_is_local(self):
        import os

        netpath._cache.clear()
        sysdrive = os.environ.get("SystemDrive", "C:")
        self.assertFalse(netpath.is_remote(sysdrive + "\\Windows\\x"))


if __name__ == "__main__":
    unittest.main()
