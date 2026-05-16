"""
PyInstaller runtime hook — HALCON DLL path setup
在任何 Python 模块导入之前运行，把捆绑的 halcon_bin 目录加入
DLL 搜索路径，确保打包的 HALCON 24.11.1 DLL 优先于系统已安装版本加载。
"""
import os
import sys

if hasattr(sys, '_MEIPASS'):
    halcon_bin = os.path.join(sys._MEIPASS, 'halcon_bin')
    if os.path.isdir(halcon_bin):
        # Python 3.8+ Windows: 通过 AddDllDirectory 注册搜索目录
        # LOAD_LIBRARY_SEARCH_DEFAULT_DIRS 会优先搜索这里
        if hasattr(os, 'add_dll_directory'):
            os.add_dll_directory(halcon_bin)
        # 同时加入 PATH 作为兜底（对某些旧版加载方式有效）
        os.environ['PATH'] = halcon_bin + os.pathsep + os.environ.get('PATH', '')
