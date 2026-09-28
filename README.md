# Matroska 时码庭

面向 Matroska/WebM EBML 结构、Cluster/Cue 与多轨帧时间证据的离线可追溯审查工具。

## 安装

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

## 测试与演示

```bash
.venv/bin/python -m pytest -q; .venv/bin/uvicorn app:app --host 127.0.0.1 --port 5375
```

然后访问 <http://127.0.0.1:5375>。首次打开会从仓库内离线案例包导入演示案；页面中的短样例路径会依次展示来源、异常、分支裁定与重放结果。
