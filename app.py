"""
Competitor Ad Intelligence Hub (Streamlit 版)

思路：
- 数据源：本地 Apify 导出的 Facebook Ads JSON（Mock，防止 API 没额度）
- 清洗与聚合：双轨制 URL 去重 + 文案指纹，计算 Intensity（热度）
- 多模态分析：下载 Top3 图片 + 文案，上传到 Gemini，生成 Insight + Midjourney Prompt
- 前端：Streamlit 单页应用（顶部 AI 战略卡片 + 底部素材画廊）
"""

from __future__ import annotations

import json
import os
import time
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from dotenv import load_dotenv
from google import genai
from google.genai import types
import requests
import streamlit as st


# ================= 1. 配置与 Mock 数据 =================

load_dotenv()

st.set_page_config(
    page_title="Competitor Ad Intelligence Hub",
    layout="wide",
    page_icon="⚡️",
)


def fetch_ads_from_apify(url: str, api_token: str, results_limit: int = 50) -> List[Dict[str, Any]]:
    """
    调用 Apify Actor (facebook-ads-scraper) 爬取数据
    """
    if not api_token:
        st.error("未配置 Apify API Token")
        return []

    # Actor ID for facebook-ads-scraper.
    actor_id = "apify~facebook-ads-scraper"

    # 1. Start the actor run
    run_url = f"https://api.apify.com/v2/acts/{actor_id}/runs?token={api_token}"

    # Input configuration for the actor
    actor_input = {
        "startUrls": [{"url": url}],
        "resultsLimit": results_limit,  # Limit to avoid long waits
        "viewMode": "list",
        "renderType": "html"
    }

    try:
        with st.status("正在启动 Apify 爬虫...", expanded=True) as status:
            st.write(f"🚀 正在启动 Actor: {actor_id}...")
            resp = requests.post(run_url, json=actor_input)
            if resp.status_code != 201:
                status.update(label="Apify 启动失败", state="error")
                st.error(f"Apify start run failed: {resp.text}")
                return []

            run_data = resp.json().get("data", {})
            run_id = run_data.get("id")
            if not run_id:
                st.error("No run ID returned from Apify.")
                return []

            st.write(f"⏳ 任务已提交 (Run ID: {run_id})，正在等待完成...")

            # 2. Poll for completion
            max_retries = 100  # Prevent infinite loop (approx 5 mins)
            retry_count = 0
            while retry_count < max_retries:
                status_url = f"https://api.apify.com/v2/actor-runs/{run_id}?token={api_token}"
                status_resp = requests.get(status_url)
                status_data = status_resp.json().get("data", {})
                run_status = status_data.get("status")

                if run_status == "SUCCEEDED":
                    st.write("✅ 爬取完成！")
                    status.update(label="爬取成功", state="complete", expanded=False)
                    break
                elif run_status in ["FAILED", "ABORTED", "TIMED-OUT"]:
                    status.update(label="爬取失败", state="error")
                    st.error(f"Apify run failed with status: {run_status}")
                    return []

                time.sleep(3)  # Wait 3 seconds before next poll
                retry_count += 1
            
            if retry_count >= max_retries:
                status.update(label="爬取超时", state="error")
                st.error("Apify run timed out.")
                return []

            # 3. Fetch dataset items
            dataset_id = status_data.get("defaultDatasetId")
            if not dataset_id:
                st.error("No dataset ID found.")
                return []

            dataset_url = f"https://api.apify.com/v2/datasets/{dataset_id}/items?token={api_token}"
            st.write(f"正在获取数据集: {dataset_id}...")
            dataset_resp = requests.get(dataset_url)
            if dataset_resp.status_code == 200:
                data = dataset_resp.json()
                st.write(f"共获取 {len(data)} 条数据。")
                return data
            else:
                st.error(f"Failed to fetch dataset items: {dataset_resp.text}")
                return []

    except Exception as e:
        st.error(f"Error fetching ads from Apify: {e}")
        return []


# ================= 2. 数据清洗与去重 =================

def get_clean_url(url: str | None) -> str:
    """提取指纹 URL（去除 ? 后的参数），只用于去重，不用于展示。"""
    if not url:
        return ""
    return url.split("?", 1)[0]


@st.cache_data(show_spinner=False)
def process_ads(raw_ads: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    核心清洗与去重逻辑：
    1. 区分视频/图片广告，分别提取预览图
    2. 生成指纹 Key (文案前 50 字 + 干净图片/视频预览 URL)
    3. 聚合计算热度 (Intensity)
    
    保留字段：
    - adArchiveID, pageID, pageName, startDateFormatted
    - snapshot: body.text, ctaText, linkUrl, title, displayFormat
    - snapshot: images, videos, cards
    """
    grouped_ads: Dict[str, Dict[str, Any]] = {}

    for ad in raw_ads:
        snapshot = ad.get("snapshot", {}) or {}
        
        # --- 基础信息 ---
        ad_archive_id = ad.get("adArchiveID") or ""
        page_id = ad.get("pageID") or ""
        page_name = ad.get("pageName") or ""
        start_date = ad.get("startDateFormatted") or ""
        
        # --- Snapshot 内容 ---
        body = snapshot.get("body") or {}
        body_text = body.get("text") or ""
        
        # --- 获取原始列表 (需要先获取 cards 以便后续 fallback) ---
        cards = snapshot.get("cards") or []
        images = snapshot.get("images") or []
        videos = snapshot.get("videos") or []
        
        # 如果 body_text 为空或包含模板变量，尝试从 cards[0].body 获取（DCO/轮播广告文案）
        def is_template_variable(text: str) -> bool:
            """检测是否包含 DCO 模板变量如 {{product.brand}}"""
            return bool(text) and "{{" in text and "}}" in text
        
        if (not body_text or is_template_variable(body_text)) and cards:
            card_body = (cards[0] or {}).get("body") or ""
            # cards 中的 body 可能是字符串或字典
            if isinstance(card_body, dict):
                card_body = card_body.get("text") or ""
            if card_body and not is_template_variable(card_body):
                body_text = card_body
        
        cta_text = snapshot.get("ctaText") or "Learn More"
        link_url = snapshot.get("linkUrl") or ""
        display_format = snapshot.get("displayFormat") or ""  # VIDEO / IMAGE 等
        
        # --- A. 判断是否为视频广告 ---
        is_video = display_format == "VIDEO" or bool(videos)
        
        # --- B. 智能提取预览图 & 视频链接 ---
        preview_image_url = ""
        video_hd_url = ""
        
        if is_video:
            # 视频广告：preview 使用 videoPreviewImageUrl，附上 videoHdUrl
            if videos:
                video0 = videos[0] or {}
                preview_image_url = video0.get("videoPreviewImageUrl") or ""
                video_hd_url = video0.get("videoHdUrl") or video0.get("videoSdUrl") or ""
            # 如果 videos 为空，尝试从 cards 获取
            if not video_hd_url and cards:
                card0 = cards[0] or {}
                video_hd_url = card0.get("videoHdUrl") or card0.get("videoUrl") or ""
                if not preview_image_url:
                    preview_image_url = card0.get("videoPreviewImageUrl") or card0.get("originalImageUrl") or ""
        else:
            # 图片广告：preview 使用 originalImageUrl
            if cards:
                # 轮播卡片：取第一张即可（去重）
                card0 = cards[0] or {}
                preview_image_url = card0.get("originalImageUrl") or card0.get("resizedImageUrl") or ""
            elif images:
                img0 = images[0] or {}
                preview_image_url = img0.get("originalImageUrl") or img0.get("resizedImageUrl") or ""
        
        # --- C. 提取标题 ---
        # 优先级：snapshot.title -> cards[0].title -> 从 body_text 截取 -> 兜底 "Sponsored Ad"
        title = snapshot.get("title") or ""
        
        # 如果 title 为空或包含模板变量，从 cards 获取
        if (not title or is_template_variable(title)) and cards:
            title = (cards[0] or {}).get("title") or ""
        
        # 如果仍然无效，从 body_text 截取前 50 个字符作为标题
        if (not title or is_template_variable(title)) and body_text:
            # 取第一行或前 50 字符
            first_line = body_text.split("\n")[0].strip()
            title = first_line[:50] + ("..." if len(first_line) > 50 else "")
        
        if not title or is_template_variable(title):
            title = "Sponsored Ad"
        
        # --- D. 生成指纹 (Fingerprint) ---
        # 用于去重：文案前50字 + 干净的预览图 URL
        clean_preview_url = get_clean_url(preview_image_url)
        fingerprint_key = f"{body_text[:50]}_{clean_preview_url}"
        
        # --- E. 聚合逻辑 ---
        if fingerprint_key in grouped_ads:
            grouped_ads[fingerprint_key]["intensity"] += 1
            grouped_ads[fingerprint_key]["ad_ids"].append(ad_archive_id)
        else:
            grouped_ads[fingerprint_key] = {
                # 指纹 & 去重
                "key": fingerprint_key,
                "intensity": 1,
                "ad_ids": [ad_archive_id],
                
                # 基础信息
                "ad_archive_id": ad_archive_id,
                "page_id": page_id,
                "page_name": page_name,
                "start_date": start_date,
                
                # 创意内容
                "title": title,
                "text": body_text,
                "cta": cta_text,
                "link_url": link_url,
                "display_format": display_format,
                
                # 媒体资源
                "is_video": is_video,
                "preview_image_url": preview_image_url,  # 统一的预览图
                "video_hd_url": video_hd_url,  # 视频广告才有
                
                # 原始数据（用于详情展示）
                "cards": cards,
                "images": images,
                "videos": videos,
            }

    # 转为列表并按热度倒序排列
    return sorted(grouped_ads.values(), key=lambda x: x["intensity"], reverse=True)


# ================= 2.5 时间筛选 =================

TIME_FILTER_OPTIONS = {
    "全部": None,
    "过去 48 小时": 48,
    "过去 72 小时": 72,
    "过去 1 周": 168,
}


def filter_ads_by_time(ads: List[Dict[str, Any]], hours: int | None) -> List[Dict[str, Any]]:
    """
    根据投放开始时间过滤广告
    hours: 过去多少小时内的广告，None 表示不过滤
    """
    if hours is None:
        return ads
    
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    filtered = []
    
    for ad in ads:
        start_date_str = ad.get("start_date") or ""
        try:
            # 格式: "2025-11-03T08:00:00.000Z"
            start_date = datetime.fromisoformat(start_date_str.replace("Z", "+00:00"))
            if start_date >= cutoff:
                filtered.append(ad)
        except (ValueError, TypeError):
            # 解析失败则保留
            filtered.append(ad)
    
    return filtered


# ================= 3. 多模态 Gemini 分析 =================

SYSTEM_PROMPT = """
**Role (角色):**
你是服务于北美顶级 DTC 品牌的高级竞品情报分析师。
你精通消费者心理学、视觉设计趋势以及 Meta 广告投放策略，专注于从竞品广告中挖掘可借鉴的创意灵感和促销情报。

**Task (任务):**
深度分析输入的一组竞品广告图片和文案，重点关注：
1. 竞品当前的促销动态（折扣力度、活动主题、紧迫感营造）
2. 可借鉴的创意元素（视觉风格、文案策略、Hook 设计）
3. 整体投放策略趋势

**--- 核心分析框架 ---**

**1. 素材类型分类:**
* **设计类型:** Render(渲染) / Real Shot(实拍) / UGC风格
* **内容策略:** Traffic(种草) / Promotion(大促) / Conversion(转化)

**2. 创意拆解维度:**
* **视觉亮点:** Hook元素、场景、结构、可借鉴点
* **文案亮点:** 框架(PAS/BAB)、情绪触发词、目标受众、可借鉴点

**3. 促销情报:** 折扣力度、活动名称、紧迫感元素

**--- OUTPUT FORMAT ---**

输出严格的 JSON（不要用 Markdown 代码块包裹）：

{
  "overall_analysis": {
    "promotion_intel": "竞品当前促销动态总结",
    "creative_trend": "创意风格趋势总结",
    "key_takeaways": "可借鉴的核心要点（列出2-3条）"
  },
  "individual_ads": [
    {
      "index": 0,
      "category": {
        "design_type": "Render - 场景渲染",
        "content_strategy": "Promotion"
      },
      "visual_highlights": {
        "hook_element": "第一眼看到的是...",
        "scene": "户外露营场景",
        "structure": "产品特写+促销文字",
        "worth_learning": "可借鉴点：..."
      },
      "copy_highlights": {
        "framework": "PAS",
        "target_audience": "价格敏感的户外爱好者",
        "emotional_triggers": ["Save", "Limited", "Now"],
        "worth_learning": "可借鉴点：..."
      },
      "promo_intel": {
        "discount": "$1,400 OFF (48%)",
        "campaign_name": "活动名称",
        "urgency_elements": ["Limited-time"]
      },
      "creative_score": 8,
      "one_line_summary": "一句话总结这条广告的核心卖点和可借鉴之处"
    }
  ]
}
"""


def download_image_to_temp(image_url: str) -> str | None:
    """
    下载图片到临时文件，返回文件路径
    """
    try:
        resp = requests.get(image_url, timeout=30)
        if resp.status_code != 200:
            return None
        
        # 根据 Content-Type 确定后缀
        content_type = resp.headers.get("Content-Type", "image/jpeg")
        if "png" in content_type:
            suffix = ".png"
        elif "gif" in content_type:
            suffix = ".gif"
        elif "webp" in content_type:
            suffix = ".webp"
        else:
            suffix = ".jpg"
        
        # 保存到临时文件
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(resp.content)
            return f.name
    except Exception as e:
        st.warning(f"图片下载失败: {e}")
        return None


def upload_image_to_gemini(client: genai.Client, image_url: str) -> Any | None:
    """
    下载图片并上传到 Gemini File API
    返回可用于 generate_content 的 file 对象
    """
    temp_path = download_image_to_temp(image_url)
    if not temp_path:
        return None
    
    try:
        uploaded_file = client.files.upload(file=temp_path)
        return uploaded_file
    except Exception as e:
        st.warning(f"图片上传到 Gemini 失败: {e}")
        return None
    finally:
        # 清理临时文件
        try:
            os.unlink(temp_path)
        except:
            pass


def analyze_with_gemini(api_key: str, groups: List[Dict[str, Any]]) -> Dict[str, Any] | None:
    """
    使用 Gemini 进行多模态分析（仅图片素材 + 文案）
    通过 File API 上传图片，避免 token 浪费
    注意：暂时只分析图片广告，视频广告跳过
    """
    if not api_key:
        return None

    try:
        client = genai.Client(api_key=api_key)
        
        # 构建 prompt 内容
        contents: List[Any] = [SYSTEM_PROMPT]
        
        # 只筛选图片广告进行分析
        image_ads = [g for g in groups if not g.get("is_video", False)]
        
        if not image_ads:
            st.warning("没有图片广告可供分析，当前仅支持图片素材分析。")
            return None
        
        # 分析 Top 5 图片广告
        top = image_ads[:5]
        uploaded_count = 0
        
        for i, g in enumerate(top):
            # 添加文案描述（使用中文格式）
            contents.append(f"\n\n广告 #{i}:\n标题: {g['title']}\n文案: {g['text'][:500]}")
            
            # 上传图片到 File API
            image_url = g.get("preview_image_url")
            if image_url:
                with st.spinner(f"正在上传图片 {i+1}/{len(top)}..."):
                    uploaded_file = upload_image_to_gemini(client, image_url)
                    if uploaded_file:
                        contents.append(uploaded_file)
                        uploaded_count += 1
        
        if uploaded_count == 0:
            st.warning("没有成功上传任何图片，无法进行分析。")
            return None
        
        # 调用 Gemini
        with st.spinner(f"正在对 {uploaded_count} 张图片进行深度创意分析..."):
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=contents
            )
        
        result_text = response.text
        if not result_text:
            return None

        raw = result_text.strip()

        # 清理 ```json ``` 包裹
        if "```json" in raw:
            raw = raw.split("```json", 1)[1].split("```", 1)[0]
        elif "```" in raw:
            raw = raw.split("```", 1)[1].split("```", 1)[0]

        try:
            return json.loads(raw)
        except Exception:
            # 如果不是严格 JSON，则做兜底（使用新的 JSON 结构）
            st.warning("Gemini 返回内容不是严格 JSON，将以纯文本形式展示。")
            return {
                "overall_analysis": {
                    "promotion_intel": result_text,
                    "creative_trend": "模型未返回结构化 JSON",
                    "key_takeaways": "请检查返回内容"
                },
                "individual_ads": []
            }

    except Exception as e:
        st.error(f"Gemini API Error: {e}")
        return {
            "overall_analysis": {
                "promotion_intel": f"Gemini API Error: {str(e)}",
                "creative_trend": "Error",
                "key_takeaways": "Error"
            },
            "individual_ads": []
        }


# ================= 4. 弹窗组件 =================

@st.dialog("广告详情")
def show_ad_details(ad: Dict[str, Any]):
    st.markdown(f"### {ad['title']}")
    
    # 媒体展示
    if ad['is_video'] and ad['video_hd_url']:
        st.video(ad['video_hd_url'])
    elif ad['preview_image_url']:
        st.image(ad['preview_image_url'], use_container_width=True)
    
    # 基础信息
    col1, col2 = st.columns(2)
    with col1:
        st.markdown(f"**广告主:** {ad['page_name']}")
        st.markdown(f"**格式:** {ad['display_format'] or 'N/A'}")
    with col2:
        st.markdown(f"**CTA:** {ad['cta']}")
        st.markdown(f"**投放日期:** {ad['start_date']}")
    
    st.divider()
    
    # 文案
    st.markdown("**📝 完整文案:**")
    st.write(ad["text"])
    
    # 链接
    if ad['link_url']:
        st.markdown(f"**🔗 落地页:** [{ad['link_url'][:50]}...]({ad['link_url']})")
    
    # 视频链接
    if ad['is_video'] and ad['video_hd_url']:
        st.markdown(f"**🎥 视频链接:** [观看视频]({ad['video_hd_url']})")
    
    # 详细数据
    with st.expander("🔍 查看原始数据"):
        st.json(ad)


# ================= 4. 主界面 UI =================

st.title("🚀 Competitor Ad Intelligence Hub (V3.0)")
st.markdown("### 全行业通用版 | 多模态 AI 分析")

# --- Sidebar ---
with st.sidebar:
    st.header("⚙️ 配置中心")
    
    # 优先读取 st.secrets, 其次 os.getenv (支持 .env)
    secrets_gemini = st.secrets.get("GEMINI_API_KEY") or ""
    secrets_apify = st.secrets.get("APIFY_API_TOKEN") or ""
    env_gemini = os.getenv("GEMINI_API_KEY") or ""
    env_apify = os.getenv("APIFY_API_TOKEN") or ""
    
    # 如果 secrets 中已配置，则隐藏输入框，直接使用 secrets
    has_secrets = bool(secrets_gemini and secrets_apify)
    
    if has_secrets:
        # Secrets 已配置，不显示输入框
        gemini_key = secrets_gemini
        apify_token = secrets_apify
        st.success("✅ API 已配置，可直接使用")
    else:
        # 未配置 secrets，显示输入框让用户手动输入
        default_gemini = secrets_gemini or env_gemini
        default_apify = secrets_apify or env_apify
        
        gemini_key = st.text_input("Gemini API Key", value=default_gemini, type="password")
        apify_token = st.text_input("Apify API Token", value=default_apify, type="password")
    
    st.divider()
    
    results_limit = st.number_input("爬取数量限制 (Max Results)", min_value=1, max_value=500, value=10, step=10)
    
    # 时间筛选
    time_filter_label = st.selectbox(
        "⏱️ 时间筛选",
        options=list(TIME_FILTER_OPTIONS.keys()),
        index=0
    )
    time_filter_hours = TIME_FILTER_OPTIONS[time_filter_label]

    st.divider()
    if not has_secrets:
        st.info("💡 请输入 Apify Token 以调用爬虫，以及 Gemini Key 进行分析。")

# --- 初始化 Session State ---
if "processed_ads" not in st.session_state:
    st.session_state.processed_ads = []
if "ai_report" not in st.session_state:
    st.session_state.ai_report = None
if "brand_library" not in st.session_state:
    st.session_state.brand_library = []
if "current_scan_url" not in st.session_state:
    st.session_state.current_scan_url = ""

# --- Tabs ---
tab_quick_scan, tab_brand_library = st.tabs(["🔍 Quick Scan", "📚 Brand Library"])


# ================= Helper: 渲染广告结果 =================
def render_ad_results(ads: List[Dict[str, Any]], ai_report: Dict[str, Any] | None, key_prefix: str = ""):
    """渲染 AI 分析结果和广告画廊（合并展示）"""
    
    # --- 整体策略分析 ---
    if ai_report:
        st.subheader("🤖 竞品情报总览")
        overall = ai_report.get("overall_analysis", {})
        
        col1, col2, col3 = st.columns(3)
        with col1:
            st.markdown("**� 促销动态**")
            st.info(overall.get("promotion_intel", "暂无数据"))
        with col2:
            st.markdown("**🎨 创意趋势**")
            st.success(overall.get("creative_trend", "暂无数据"))
        with col3:
            st.markdown("**💡 可借鉴要点**")
            st.warning(overall.get("key_takeaways", "暂无数据"))
        
        st.divider()
    
    # --- 素材画廊 + 分析合并展示 ---
    st.subheader(f"🔥 素材分析库 ({len(ads)} 个创意)")
    
    # 构建 index -> 分析结果的映射
    analysis_map = {}
    if ai_report:
        for ad_analysis in ai_report.get("individual_ads", []):
            idx = ad_analysis.get("index", -1)
            if idx >= 0:
                analysis_map[idx] = ad_analysis
    
    cols = st.columns(3)
    for idx, ad in enumerate(ads):
        with cols[idx % 3]:
            with st.container(border=True):
                # 媒体预览
                if ad["preview_image_url"]:
                    st.image(ad["preview_image_url"], use_container_width=True)
                else:
                    st.text("No Preview")

                # 标题与热度
                st.markdown(f"**{ad['title']}**")
                st.caption(f"� 热度: {ad['intensity']} | 📅 {ad['start_date'][:10] if ad['start_date'] else 'N/A'}")
                
                # 视频/图片标签
                if ad["is_video"]:
                    st.caption(f"🎥 Video | by {ad['page_name']}")
                else:
                    st.caption(f"🖼️ Image | by {ad['page_name']}")

                # 文案预览
                preview = (ad["text"] or "")[:80]
                st.text(preview + ("..." if len(ad["text"] or "") > 80 else ""))
                
                # --- AI 分析结果（如果有）---
                if idx in analysis_map:
                    analysis = analysis_map[idx]
                    category = analysis.get("category", {})
                    visual = analysis.get("visual_highlights", {})
                    copy_hl = analysis.get("copy_highlights", {})
                    promo = analysis.get("promo_intel", {})
                    score = analysis.get("creative_score", 0)
                    summary = analysis.get("one_line_summary", "")
                    
                    # 一句话总结
                    if summary:
                        st.success(f"� {summary}")
                    
                    # 展开查看详细分析
                    with st.expander("📊 查看详细分析"):
                        # 分类标签
                        st.markdown(f"**类型:** {category.get('design_type', 'N/A')} | {category.get('content_strategy', 'N/A')}")
                        st.markdown(f"**创意评分:** {'⭐' * min(score, 10)} ({score}/10)")
                        
                        # 促销情报
                        if promo:
                            discount = promo.get('discount', '')
                            campaign = promo.get('campaign_name', '')
                            urgency = promo.get('urgency_elements', [])
                            if discount or campaign:
                                st.markdown(f"**💰 促销:** {discount} | {campaign}")
                            if urgency:
                                st.markdown(f"**⏰ 紧迫感:** {', '.join(urgency)}")
                        
                        # 视觉亮点
                        st.markdown("**�️ 视觉:**")
                        st.markdown(f"- Hook: {visual.get('hook_element', 'N/A')}")
                        st.markdown(f"- 场景: {visual.get('scene', 'N/A')}")
                        if visual.get('worth_learning'):
                            st.markdown(f"- ✨ {visual.get('worth_learning')}")
                        
                        # 文案亮点
                        st.markdown("**📝 文案:**")
                        st.markdown(f"- 框架: {copy_hl.get('framework', 'N/A')} | 受众: {copy_hl.get('target_audience', 'N/A')}")
                        triggers = copy_hl.get('emotional_triggers', [])
                        if triggers:
                            st.markdown(f"- 情绪词: {', '.join(triggers)}")
                        if copy_hl.get('worth_learning'):
                            st.markdown(f"- ✨ {copy_hl.get('worth_learning')}")

                # 详情按钮
                if st.button("🔍 查看原始详情", key=f"{key_prefix}btn_{idx}"):
                    show_ad_details(ad)


# ================= Tab 1: Quick Scan =================
with tab_quick_scan:
    url_input = st.text_input(
        "Facebook Ad Library URL", 
        placeholder="https://www.facebook.com/ads/library/?...",
        key="quick_scan_url"
    )
    
    if st.button("🚀 开始分析 (Start Scan)", type="primary", key="quick_scan_btn"):
        if not url_input:
            st.error("请输入 URL")
        else:
            # 1) 数据获取
            raw_data = fetch_ads_from_apify(url_input, apify_token, results_limit)
            
            if not raw_data:
                st.warning("未获取到数据，请检查 URL 或 Token。")
            else:
                # 2) 清洗与聚合
                processed_ads = process_ads(raw_data)
                
                # 3) 时间筛选
                filtered_ads = filter_ads_by_time(processed_ads, time_filter_hours)
                
                st.session_state.processed_ads = filtered_ads
                st.session_state.current_scan_url = url_input
                
                if time_filter_hours:
                    st.success(f"✅ 抓取成功，共 {len(processed_ads)} 个创意，筛选后 {len(filtered_ads)} 个")
                else:
                    st.success(f"✅ 抓取成功，共清洗出 {len(filtered_ads)} 个独立创意")

                # 4) AI 分析
                if gemini_key and filtered_ads:
                    with st.spinner("正在调用 Gemini 进行 AI 策略分析..."):
                        st.session_state.ai_report = analyze_with_gemini(gemini_key, filtered_ads)
                else:
                    st.session_state.ai_report = None
    
    # 渲染结果
    if st.session_state.processed_ads:
        render_ad_results(
            st.session_state.processed_ads, 
            st.session_state.ai_report,
            key_prefix="qs_"
        )


# ================= Tab 2: Brand Library =================
with tab_brand_library:
    st.markdown("### 📚 品牌资产库")
    st.markdown("保存常用品牌的广告库链接，方便快速分析。")
    
    # --- 添加品牌 ---
    with st.expander("➕ 添加新品牌", expanded=True):
        col1, col2 = st.columns([1, 2])
        with col1:
            new_brand_name = st.text_input("品牌名称", placeholder="例如: Jackery", key="new_brand_name")
        with col2:
            new_brand_url = st.text_input("Ad Library URL", placeholder="https://www.facebook.com/ads/library/?...", key="new_brand_url")
        
        if st.button("💾 保存品牌", key="save_brand_btn"):
            if new_brand_name and new_brand_url:
                # 检查是否重复
                existing_names = [b["name"] for b in st.session_state.brand_library]
                if new_brand_name in existing_names:
                    st.warning(f"品牌 '{new_brand_name}' 已存在")
                else:
                    st.session_state.brand_library.append({
                        "name": new_brand_name,
                        "url": new_brand_url,
                        "added_at": datetime.now().isoformat()
                    })
                    st.success(f"✅ 已保存品牌: {new_brand_name}")
                    st.rerun()
            else:
                st.error("请填写品牌名称和 URL")
    
    st.divider()
    
    # --- 品牌列表 ---
    if st.session_state.brand_library:
        st.markdown("### 已保存的品牌")
        
        for idx, brand in enumerate(st.session_state.brand_library):
            with st.container(border=True):
                col1, col2, col3 = st.columns([2, 1, 1])
                with col1:
                    st.markdown(f"**{brand['name']}**")
                    st.caption(f"添加时间: {brand['added_at'][:10]}")
                with col2:
                    if st.button("🔍 分析", key=f"analyze_brand_{idx}"):
                        # 触发分析
                        with st.spinner(f"正在分析 {brand['name']}..."):
                            raw_data = fetch_ads_from_apify(brand["url"], apify_token, results_limit)
                            if raw_data:
                                processed = process_ads(raw_data)
                                filtered = filter_ads_by_time(processed, time_filter_hours)
                                st.session_state.processed_ads = filtered
                                st.session_state.current_scan_url = brand["url"]
                                
                                if gemini_key and filtered:
                                    st.session_state.ai_report = analyze_with_gemini(gemini_key, filtered)
                                else:
                                    st.session_state.ai_report = None
                                
                                st.success(f"✅ 已加载 {brand['name']} 的 {len(filtered)} 个广告")
                                st.rerun()
                            else:
                                st.error("获取数据失败")
                with col3:
                    if st.button("🗑️ 删除", key=f"delete_brand_{idx}"):
                        st.session_state.brand_library.pop(idx)
                        st.rerun()
        
        # 显示当前分析结果
        if st.session_state.processed_ads and st.session_state.current_scan_url:
            st.divider()
            # 找到当前品牌名
            current_brand = next(
                (b["name"] for b in st.session_state.brand_library 
                 if b["url"] == st.session_state.current_scan_url), 
                "Unknown"
            )
            st.markdown(f"### 📊 {current_brand} 分析结果")
            render_ad_results(
                st.session_state.processed_ads,
                st.session_state.ai_report,
                key_prefix="bl_"
            )
    else:
        st.info("📭 暂无保存的品牌，请添加第一个品牌开始使用。")

