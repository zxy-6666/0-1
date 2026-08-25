"""统一路径解析

目标：无论以源码方式运行，还是被打包成可执行文件（PyInstaller），都把
用户数据放在**可执行程序所在目录**（“根目录”）下：
  - data/   输入配置表（flow.csv、step_ct.csv …）  —— 用户可在此直接增删改
  - output/ 排程结果 / 快照 / Excel / 甘特图 PNG
  - static/ 运行期生成的静态图片（甘特 PNG 落盘处）

打包后程序是否还要装 Python？不需要：PyInstaller 已把 Python 解释器与依赖
一起打进单个可执行文件，双击即可运行、自动打开浏览器。
"""
from __future__ import annotations

import os
import sys


def _app_root() -> str:
    """返回“根目录”：可执行程序所在目录（打包时）或项目根目录（源码运行）。"""
    if getattr(sys, "frozen", False):
        # PyInstaller 打包后：exe/.app 所在目录
        return os.path.dirname(os.path.abspath(sys.executable))
    # 源码运行：paths.py 位于项目根目录，根目录即其所在目录
    return os.path.dirname(os.path.abspath(__file__))


def _templates_dir() -> str:
    """模板目录：打包时用 _MEIPASS 里已捆绑的 templates，源码时用 web/templates。"""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, "templates")
    return os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "templates"))


APP_ROOT = _app_root()
TEMPLATES_DIR = _templates_dir()
DATA_DIR = os.path.join(APP_ROOT, "data")
OUTPUT_DIR = os.path.join(APP_ROOT, "output")
STATIC_DIR = os.path.join(APP_ROOT, "static")
SNAPSHOT_DIR = os.path.join(OUTPUT_DIR, "snapshots")

# 确保关键用户数据目录存在
for _d in (DATA_DIR, OUTPUT_DIR, SNAPSHOT_DIR, STATIC_DIR):
    os.makedirs(_d, exist_ok=True)