# 第三方代码说明

本仓库以 MIT 许可发布自有代码。以下文件包含来自其它项目的代码，按要求保留来源说明。

## 1. `backend/app/services/pdf_export.py`

- 来源：MechResearch-Agent（`backend/src/services/pdf_export.py`），该项目在 `pyproject.toml` 中声明 MIT 许可。
- 用途：把 Markdown 报告渲染为 PDF（纯标准库实现，无 reportlab 依赖）。
- 修改：未改动实现，仅放置于本仓库 `services/` 包内，由 `app/graph/main_graph.py` 调用 `build_pdf_bytes()`。
- 说明：原项目未附独立 LICENSE 文件，此处依据其包元数据声明；如上游补充许可文件，以该文件为准。

```text
MIT License

Copyright (c) MechResearch-Agent contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## 2. 仅借鉴思路、未复制代码

- CFDFlow-Agent-LangGraph-Context：LangGraph 节点组织方式、工具 JSON 返回协议（`status/text/data/error`）。该仓库未声明项目级许可，因此**未复制其源码**；本仓库的 `tool_protocol` 语义为独立重写。
- MechResearch-Agent：SSE 前端分帧读取思路、`ToolCallTracker` 事件模型。本仓库以 fetch + `id/event/data` 解析重写为 `useRunStream.ts`，未复制原文件。

## 3. 依赖

运行时依赖（FastAPI、LangGraph、numpy、openai、pypdf 等）以 PyPI 包形式引入，未在仓库内二次分发，各自遵循其上游许可。
