"""查看/清理 NSIS 安装器在注册表里留下的安装记忆。

Tauri 的 NSIS 脚本会把 InstallDir 写进 HKCU\\...\\Uninstall\\<key>，
下次运行安装器时会直接复用该路径（即使没带 /D=）。
本脚本用于：
  list  列出 CleanTV-RS 相关注册表项
  reset 删除安装记忆（让下次安装回到默认路径 %LOCALAPPDATA%\\CleanTV-RS）
"""
import sys

import winreg

UNINSTALL = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"

# ★ Tauri NSIS 的「上次安装目录」记忆**不在** Uninstall 键里，而在
#   HKCU\Software\<MANUFACTURER>\<PRODUCTNAME> 的**默认值**（键名是空串）。
#   installer.nsi 的 RestorePreviousInstallLocation 会读它并覆盖默认安装路径
#   → 就算删了 Uninstall 键 + 不带 /D=，安装器还是会装回老目录。
#   必须把这个键一起删掉才能验证「默认路径安装」。
MEMORY_KEY = r"Software\clean\CleanTV-RS"


def find_keys():
    root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL)
    hits = []
    i = 0
    while True:
        try:
            sub = winreg.EnumKey(root, i)
        except OSError:
            break
        i += 1
        try:
            k = winreg.OpenKey(root, sub)
        except OSError:
            continue
        try:
            dn = str(winreg.QueryValueEx(k, "DisplayName")[0])
        except OSError:
            dn = ""
        if "clean" in dn.lower() or "clean" in sub.lower():
            hits.append((sub, k, dn))
    return hits


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "list"
    hits = find_keys()
    if not hits:
        print("没有 CleanTV 相关注册表项")
        return 0
    for sub, k, dn in hits:
        print("KEY: %s | DisplayName: %s" % (sub, dn))
        for v in ("InstallLocation", "UninstallString", "QuietUninstallString",
                  "DisplayVersion", "Publisher"):
            try:
                print("    %s = %s" % (v, winreg.QueryValueEx(k, v)[0]))
            except OSError:
                pass
        if mode == "reset":
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL + "\\" + sub)
                print("    -> Uninstall 键已删除")
            except OSError as e:
                print("    -> Uninstall 键删除失败: %s" % e)
    if mode == "reset":
        # 删「上次安装目录」记忆键
        try:
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, MEMORY_KEY)
            print("MEMORY %s:" % MEMORY_KEY)
            j = 0
            while True:
                try:
                    val = winreg.EnumValue(k, j)
                except OSError:
                    break
                j += 1
                print("    %r = %r" % (val[0], val[1]))
            k.Close()
        except OSError:
            print("MEMORY %s: 不存在" % MEMORY_KEY)
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, MEMORY_KEY)
            print("    -> 记忆键已删除")
        except OSError as e:
            print("    -> 记忆键删除失败: %s" % e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
