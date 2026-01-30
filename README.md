## Competitor Ad Intelligence Hub (Streamlit 版)

这是一个单文件的 Streamlit 应用（`app.py`），用于：

- **聚合与去重竞品广告素材**（双轨制 URL 去重 + 热度 Intensity）
- 使用 **Google Gemini 多模态能力**（看图 + 读文案）生成：
  - 竞品投放策略洞察（Insight）
  - 面向 Midjourney 的创意升级 Prompt

### 1. 环境准备

- 安装 Python 3.10+（推荐）
- 在项目根目录安装依赖：

```bash
pip install -r requirements.txt
```

- 设置环境变量（可选）：你可以在运行时在 Sidebar 输入 Gemini API Key，或者在本地环境中设置：

```bash
export GEMINI_API_KEY="你的_API_Key"
```

### 2. 启动应用

在项目根目录执行：

```bash
streamlit run app.py
```

浏览器会自动打开 `http://localhost:8501`。

### 3. 主要文件说明

- `app.py`：主应用，包含：
  - Mock Jackery 广告数据
  - 清洗与聚合逻辑（指纹去重 + Intensity 排序）
  - 多模态分析流水线（下载图片、上传到 Gemini、生成 Insight 与 Prompt）
  - Streamlit UI（Sidebar 配置、顶部 AI 战略卡片、底部素材画廊）
- `.env.local`（可选）：你可以在这里存自己的本地配置，但当前应用主要通过 Sidebar 录入 API Key。
- `code.ipynb`：你自己的 Notebook 草稿，不参与应用运行。

> 说明：原始的 React / Vite / TypeScript 前端模板文件已经移除，仅保留一个 `legacy_frontend` 目录（如果存在）用于备份参考；当前运行逻辑完全基于 Streamlit。

