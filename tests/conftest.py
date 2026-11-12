# -*- coding: utf-8 -*-
"""pytest 公共夹具：把仓库根加进 sys.path（tests 子目录运行时需要）。"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
