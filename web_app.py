# web_app.py
import os
import re
import json
import base64
import time
import requests
import io
import html as html_lib
from datetime import datetime
from urllib.parse import quote
import markdown
import fitz  # PyMuPDF
from PIL import Image
from openai import OpenAI
import streamlit as st

# ==========================================
# 预设常量与配置 (完整保留自原脚本)
# ==========================================
API_REGISTRY = {
    "cursor": {
        "name": "CursorAI",
        "url": "https://api.cursorai.live/v1",
        "key": "sk-rTAmhMFzZFlVFEAg5Qtddt6IKMICGrkpwfj85l80Mq5Vu6It",
        "models": ["gemini-3.1-pro-preview", "gemini-3.1-flash-lite-preview", "claude-sonnet-4-6", "mimo-v2.5", "gpt-5.5","gpt-5.4","gpt-5.4-mini", "gpt-5.4-nano", "grok-4-20-non-reasoning", "grok-4-20-reasoning"]
    },
    "silicon": {
        "name": "SiliconFlow",
        "url": "https://api.siliconflow.cn/v1",
        "key": "sk-xreqefxvzupbmjkvqmypbzytekzhubykcbqrophbsenrxwyl",
        "models": ["deepseek-ai/DeepSeek-R1", "deepseek-ai/DeepSeek-V4-Flash", "Pro/moonshotai/Kimi-K2.6"]
    },
    "zhiyun": {
        "name": "知云",
        "url": "https://alice.isrna.cn/v1",
        "key": "sk-0RkhJt153ACh32rS3g5fd0SNM64V344Y2C433wbr",
        "models": ["deepseek-v4-pro","deepseek-v4-flash","gemini-3.1-pro", "gemini-3-flash", "gpt-5.4"]
    }
}

CONFIG_FILE = "analyzer_config.json"
UPLOAD_URL = "http://pi.3body.top/paper/upload.php"
WIZ_URL = "http://wiz.3body.top"
WIZ_USER = "admin@wiz.cn"
WIZ_PASS = "09201075"
NETWORK_RETRY_TIMES = 2
NETWORK_RETRY_INTERVAL = 3
WIZ_IMAGE_MAX_WIDTH = 900
WIZ_IMAGE_QUALITY = 60
HTML_IMAGE_MAX_WIDTH = 1200
HTML_IMAGE_QUALITY = 82
HTML_IMAGE_MIN_QUALITY = 68
HTML_IMAGE_TARGET_BYTES = 900 * 1024
WIZ_ARTICLE_NOTE_CATEGORY = "/文章笔记/"

# ==========================================
# 辅助类与函数
# ==========================================
class MultipartUploadStream:
    def __init__(self, parts, callback=None):
        self.parts = parts
        self.callback = callback
        self.total = sum(len(part) for part in parts)
        self.position = 0
        self.part_index = 0
        self.part_offset = 0
        self.last_report = 0

    def read(self, size=-1):
        if self.position >= self.total:
            return b''
        if size is None or size < 0:
            size = self.total - self.position
        chunks = []
        remaining = size
        while remaining > 0 and self.part_index < len(self.parts):
            part = self.parts[self.part_index]
            if self.part_offset >= len(part):
                self.part_index += 1
                self.part_offset = 0
                continue
            take = min(remaining, len(part) - self.part_offset)
            chunks.append(part[self.part_offset:self.part_offset + take])
            self.part_offset += take
            self.position += take
            remaining -= take
        data = b''.join(chunks)
        if self.callback and data:
            if self.position == self.total or self.position - self.last_report >= 256 * 1024:
                self.last_report = self.position
                self.callback(self.position, self.total)
        return data

# ==========================================
# 核心解析引擎 (剥离界面后的纯逻辑类)
# ==========================================
class PaperAnalyzerEngine:
    def __init__(self, config, log_cb, stream_cb, progress_cb):
        self.config = config
        self.log_cb = log_cb
        self.stream_cb = stream_cb
        self.progress_cb = progress_cb

    def safe_log(self, text):
        if self.log_cb:
            self.log_cb(f"[{datetime.now().strftime('%H:%M:%S')}] {text}")

    def safe_stream_append(self, text):
        if self.stream_cb:
            self.stream_cb(text)

    def set_progress(self, value=None):
        if self.progress_cb and value is not None:
            self.progress_cb(value)

    def fetch_wiznote_max_number(self):
        try:
            session = requests.Session()
            login_resp = session.post(f"{WIZ_URL}/as/user/login", json={"userId": WIZ_USER, "password": WIZ_PASS}, timeout=15)
            login_resp.raise_for_status()
            login_data = login_resp.json()
            if login_data.get("returnCode") != 200:
                return None
            
            token = login_data.get("result", {}).get("token")
            kb_guid = "00000000-0000-0000-0000-000000000000"
            headers = {"X-Wiz-Token": token}
            
            notes_resp = session.post(f"{WIZ_URL}/ks/note/list/{kb_guid}", headers=headers, json={"category": WIZ_ARTICLE_NOTE_CATEGORY}, timeout=30)
            notes_resp.raise_for_status()
            notes_data = notes_resp.json()
            
            if notes_data.get("returnCode") != 200:
                return None
            
            notes = notes_data.get("result", {}).get("data", [])
            max_num = 0
            for note in notes:
                title = note.get("title", "")
                match = re.match(r'^(\d+)-', title)
                if match:
                    try:
                        num = int(match.group(1))
                        if num > max_num:
                            max_num = num
                    except ValueError:
                        continue
            if max_num > 0:
                return max_num
            return None
        except Exception:
            return None

    def truncate_references(self, text):
        patterns = [
            r'\n\s*(?:[0-9]{1,2}\.?\s*)?REFERENCES\s*\n',
            r'\n\s*(?:[0-9]{1,2}\.?\s*)?References\s*\n',
            r'\n\s*(?:[0-9]{1,2}\.?\s*)?Literature Cited\s*\n',
            r'\n\s*(?:[0-9]{1,2}\.?\s*)?Bibliography\s*\n'
        ]
        text_length = len(text)
        for pattern in patterns:
            matches = list(re.finditer(pattern, text))
            if matches:
                for match in reversed(matches):
                    if match.start() > text_length * 0.5:
                        return text[:match.start()]
        return text

    def compress_image_for_html(self, img_data, source_ext='png', max_width=HTML_IMAGE_MAX_WIDTH, quality=HTML_IMAGE_QUALITY, min_quality=HTML_IMAGE_MIN_QUALITY, target_bytes=HTML_IMAGE_TARGET_BYTES):
        original_size = len(img_data)
        source_ext = 'jpeg' if source_ext.lower() in ('jpg', 'jpeg') else source_ext.lower()
        try:
            img = Image.open(io.BytesIO(img_data))
            img.load()
            if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
                img = img.convert('RGBA')
                bg = Image.new('RGB', img.size, (255, 255, 255))
                bg.paste(img, mask=img.split()[3])
                img = bg
            elif img.mode not in ('RGB', 'L'):
                img = img.convert('RGB')
            if img.width > max_width:
                ratio = max_width / img.width
                img = img.resize((max_width, max(1, int(img.height * ratio))), Image.LANCZOS)
            best_data = None
            best_quality = quality
            for current_quality in range(quality, min_quality - 1, -6):
                img_byte_arr = io.BytesIO()
                img.save(img_byte_arr, format='JPEG', quality=current_quality, optimize=True, progressive=True)
                current_data = img_byte_arr.getvalue()
                best_data = current_data
                best_quality = current_quality
                if len(current_data) <= target_bytes:
                    break
            if best_data and len(best_data) < original_size:
                return best_data, 'jpeg', original_size, len(best_data), best_quality
        except Exception:
            pass
        return img_data, source_ext, original_size, original_size, None

    def extract_pdf_data(self, pdf_path):
        text = ""
        figures = {}
        self.safe_log("正在提取 PDF 纯文本及图像...")
        with fitz.open(pdf_path) as doc:
            for page_num in range(len(doc)):
                page = doc[page_num]
                text += page.get_text() + "\n"
                
                for img_idx, img in enumerate(page.get_images()):
                    xref = img[0]
                    try:
                        pix = fitz.Pixmap(doc, xref)
                        if pix.n - pix.alpha >= 4:
                            pix = fitz.Pixmap(fitz.csRGB, pix)
                            
                        width, height = pix.width, pix.height
                        area = width * height
                        aspect_ratio = width / height if height > 0 else 0
                        is_valid_figure = True
                        
                        if width < 300 or height < 300: is_valid_figure = False
                        if aspect_ratio > 3.5 or aspect_ratio < 0.28: is_valid_figure = False
                        
                        page_rect = page.rect
                        rects = page.get_image_rects(xref)
                        img_rect = rects[0] if rects else None
                        if img_rect:
                            y_center = (img_rect.y0 + img_rect.y1) / 2
                            if y_center < page_rect.height * 0.1 or y_center > page_rect.height * 0.9:
                                is_valid_figure = False
                            img_area_ratio = (img_rect.width * img_rect.height) / (page_rect.width * page_rect.height)
                            if img_area_ratio > 0.8:
                                is_valid_figure = False
                        
                        if page_num == 0 and area < 450000: is_valid_figure = False
                        if page_num < 2 and area > 3000000: is_valid_figure = False
                            
                        if is_valid_figure:
                            img_data = pix.tobytes("png")
                            img_data, img_ext, original_size, compressed_size, img_quality = self.compress_image_for_html(img_data)
                            key = len(figures) + 1
                            figures[key] = {'data': img_data, 'ext': img_ext, 'page': page_num + 1, 'original_size': original_size, 'compressed_size': compressed_size, 'quality': img_quality}
                        pix = None
                    except Exception as e:
                        pass

        original_length = len(text)
        text = self.truncate_references(text)
        saved_chars = original_length - len(text)
        if saved_chars > 0:
            self.safe_log(f"已自动裁切参考文献部分，节省 {saved_chars} 个字符 Token。")
        original_img_size = sum(fig.get('original_size', len(fig.get('data', b''))) for fig in figures.values())
        compressed_img_size = sum(fig.get('compressed_size', len(fig.get('data', b''))) for fig in figures.values())
        if original_img_size > 0 and compressed_img_size < original_img_size:
            saved_mb = (original_img_size - compressed_img_size) / 1024 / 1024
            self.safe_log(f"图片已清晰压缩：{original_img_size / 1024 / 1024:.2f} MB → {compressed_img_size / 1024 / 1024:.2f} MB，节省 {saved_mb:.2f} MB。")
        self.safe_log(f"资源提取完成：捕获 {len(figures)} 张图表，纯文本约 {len(text)} 字。")
        return text, figures

    def extract_epub_data(self, epub_path):
        text = ""
        figures = {}
        self.safe_log("正在提取 EPUB 纯文本及图像资产...")
        try:
            with open(epub_path, "rb") as f:
                epub_buffer = f.read()
            with fitz.open(stream=epub_buffer, filetype="epub") as doc:
                for page_num in range(len(doc)):
                    page = doc[page_num]
                    text += page.get_text() + "\n"
                    for img_idx, img in enumerate(page.get_images()):
                        xref = img[0]
                        try:
                            pix = fitz.Pixmap(doc, xref)
                            if pix.n - pix.alpha >= 4:
                                pix = fitz.Pixmap(fitz.csRGB, pix)
                            width, height = pix.width, pix.height
                            if width >= 150 and height >= 150:
                                img_data = pix.tobytes("png")
                                img_data, img_ext, original_size, compressed_size, img_quality = self.compress_image_for_html(img_data)
                                key = len(figures) + 1
                                figures[key] = {'data': img_data, 'ext': img_ext, 'page': page_num + 1, 'original_size': original_size, 'compressed_size': compressed_size, 'quality': img_quality}
                            pix = None
                        except Exception: pass
        except Exception:
            self.safe_log(f"⚠️ 标准引擎拒载该 EPUB，启动兼容提取模式...")
            import zipfile
            text = ""  
            figures = {}
            try:
                with zipfile.ZipFile(epub_path, 'r') as z:
                    html_files = [f for f in z.namelist() if f.lower().endswith(('.html', '.xhtml', '.htm'))]
                    for html_file in html_files:
                        try:
                            content = z.read(html_file).decode('utf-8', errors='ignore')
                            clean_text = re.sub(r'<style.*?</style>', '', content, flags=re.DOTALL | re.IGNORECASE)
                            clean_text = re.sub(r'<script.*?</script>', '', clean_text, flags=re.DOTALL | re.IGNORECASE)
                            clean_text = re.sub(r'<[^>]+>', ' ', clean_text)
                            clean_text = re.sub(r'\s+', ' ', clean_text).strip()
                            if clean_text: text += clean_text + "\n\n"
                        except Exception: continue
                    image_files = [f for f in z.namelist() if f.lower().endswith(('.png', '.jpg', '.jpeg'))]
                    for img_file in image_files:
                        img_data = z.read(img_file)
                        if len(img_data) > 15360:
                            source_ext = img_file.split('.')[-1].lower()
                            img_data, img_ext, original_size, compressed_size, img_quality = self.compress_image_for_html(img_data, source_ext=source_ext)
                            key = len(figures) + 1
                            figures[key] = {'data': img_data, 'ext': img_ext, 'page': '内部文件', 'original_size': original_size, 'compressed_size': compressed_size, 'quality': img_quality}
                if not text.strip(): raise RuntimeError("未发现有效文本。")
            except Exception as fallback_err:
                raise RuntimeError(f"文件结构异常: {str(fallback_err)}")

        original_length = len(text)
        text = self.truncate_references(text)
        saved_chars = original_length - len(text)
        if saved_chars > 0: self.safe_log(f"已尝试裁切参考文献，节省 {saved_chars} 个字符 Token。")
        original_img_size = sum(fig.get('original_size', len(fig.get('data', b''))) for fig in figures.values())
        compressed_img_size = sum(fig.get('compressed_size', len(fig.get('data', b''))) for fig in figures.values())
        if original_img_size > 0 and compressed_img_size < original_img_size:
            saved_mb = (original_img_size - compressed_img_size) / 1024 / 1024
            self.safe_log(f"图片已清晰压缩：{original_img_size / 1024 / 1024:.2f} MB → {compressed_img_size / 1024 / 1024:.2f} MB，节省 {saved_mb:.2f} MB。")
        self.safe_log(f"资源提取完成：捕获 {len(figures)} 张图表，纯文本约 {len(text)} 字。")
        return text, figures

    def get_current_api_info(self):
        selected_provider_name = self.config.get("provider")
        for data in API_REGISTRY.values():
            if data["name"] == selected_provider_name:
                return data["url"], data["key"]
        return "", ""

    def call_llm_api(self, text):
        target_model = self.config.get("model")
        api_url, api_key = self.get_current_api_info()

        if not api_url or not api_key:
            self.safe_log("❌ 未找到对应的 API 服务商信息。")
            return None

        client = OpenAI(api_key=api_key, base_url=api_url)
        
        head_text = text[:1200]
        abstract_text = ""
        abstract_match = re.search(r'\babstract\b[\s\S]{0,50}?\n([\s\S]{200,4000})\n\s*(?:\d+\s*\.\s*)?introduction\b', text, re.IGNORECASE)
        if abstract_match:
            abstract_text = abstract_match.group(1).strip()
        else:
            abstract_match = re.search(r'\babstract\b[\s\S]{0,50}?\n([\s\S]{200,4000})\n\s*(?:keywords|key words)\b', text, re.IGNORECASE)
            if abstract_match:
                abstract_text = abstract_match.group(1).strip()
        
        type_detection_text = f"文章开头：\n{head_text}\n\n摘要：\n{abstract_text if abstract_text else '未能单独提取摘要，请根据文章开头判断。'}"
        type_detection_prompt = """你是学术论文类型分类助手。请根据用户提供的论文标题、文章开头和摘要，判断该论文属于哪一种类型。

分类标准：
1. Review：综述、系统综述、Meta分析、观点/展望类综述，主要目标是总结、比较、梳理已有研究。
2. Research：原创实验研究、临床研究、计算研究、方法学研究，主要目标是报告新的实验、数据、模型、算法或研究结果。

只允许输出一个英文单词：Review 或 Research。不要输出解释、标点、JSON 或 Markdown。"""
        
        try:
            self.safe_log(f"正在调用模型 [{target_model}] 判断文献类型...")
            type_response = client.chat.completions.create(
                model=target_model,
                messages=[
                    {"role": "system", "content": type_detection_prompt},
                    {"role": "user", "content": type_detection_text[:8000]}
                ],
                temperature=0,
                stream=False
            )
            detected_type = type_response.choices[0].message.content.strip()
        except Exception as e:
            self.safe_log(f"⚠️ 文献类型判断失败，默认按实验性论文处理: {str(e)}")
            detected_type = "Research"
        
        is_review = detected_type.lower().startswith("review")
        paper_type = "Review" if is_review else "Research"
        self.safe_log(f"模型判断文献类型: {'综述性论文 (Review)' if is_review else '实验性论文 (Research)'}")
        
        if is_review:
            system_prompt = """你是一个顶级的学术论文解读专家。请阅读用户提供的综述(Review)论文纯文本，并严格按照要求逐条分析这篇英文SCI论文的核心内容。

【核心准则】
1. 绝不瞎编、臆造文中未明确提及的数据。全文使用中文输出。
2. 采用多级表情符号增强视觉排版层次。
3. 遇到晦涩的专业术语，请用括号进行简短的通俗解释。
4. 核心发现和关键词请使用 Markdown的加粗语法突出显示。
5. 综述文章没有实验细节，重点在于总结前人工作、梳理逻辑框架和提出未来展望。
6. 在图表解析位置，必须准确使用 [图 1]、[图 2] 等锚点标记占位。

【输出格式要求（绝对禁止输出JSON代码块）】
请严格使用以下XML标签包裹你的输出内容，以便程序解析：

<title_cn>此处写中文标题</title_cn>
<title_en>此处写英文标题</title_en>
<journal_info>此处写杂志、发表时间</journal_info>
<authors>此处写作者及单位</authors>
<abstract>此处写摘要的完整翻译与核心主旨通俗解读</abstract>
<core_viewpoints>此处总结这篇综述提出的最核心的观点、重要发现以及其在领域内的科学意义（分点列出）</core_viewpoints>
<section_breakdown>此处进行逐节/逐段解读：详细梳理文章的主体部分，每一节（或重要段落）讲述了什么内容，列出小标题并进行深度解读。如果文中包含图表，请在对应段落末尾插入图锚点，如 [图 1]。</section_breakdown>
<overall_logic>此处分析整体逻辑架构：作者是如何组织这篇综述的，各部分之间的逻辑关联是什么</overall_logic>
<conclusion>此处写文章的最终结论与对未来研究方向的展望</conclusion>"""
        else:
            system_prompt = """你是一个顶级的学术论文解读专家。请阅读用户提供的论文纯文本，并严格按照要求逐条分析这篇英文SCI论文的核心内容。

【核心准则】
1. 绝不瞎编、臆造文中未明确提及的数据。全文使用中文输出。
2. 采用多级表情符号增强视觉排版层次。
3. 遇到晦涩的专业术语，请用括号进行简短的通俗解释。
4. 核心发现和关键词请使用 Markdown的加粗语法突出显示。
5. 在图表解析位置，必须准确使用 [图 1]、[图 2] 等锚点标记占位。

【输出格式要求（绝对禁止输出JSON代码块）】
请严格使用以下XML标签包裹你的输出内容，以便程序解析：

<title_cn>此处写中文标题</title_cn>
<title_en>此处写英文标题</title_en>
<journal_info>此处写杂志、发表时间</journal_info>
<authors>此处写作者及单位</authors>
<abstract>此处写摘要的完整翻译与核心主旨通俗解读</abstract>
<background>此处写引言段落主题及总体写作逻辑</background>
<methods>此处写实验步骤及其对应的科学目标</methods>
<results>此处为核心重点！
解读论文的每一项实验结果及其意义
请严格按照以下格式，根据文本正文对图表的描述以及提取到的图表题注（Caption），逐一解析论文中的核心图表（Figure）：
🔹 **【图 X 题注精译】**：翻译提取到的纯文本图表题注。
🔹 **【深度图解】**：结合上下文语境，解释该图表的实验设计、数据指标及说明的科学问题。
🔹 **占位锚点**：末尾插入对应的图锚点，如 [图 1]，以便后续程序注入本地图像。
</results>
<discussion>此处写讨论部分的主题与每个段落的主题以及逻辑链条</discussion>
<conclusion>此处写结论总结</conclusion>
<limitation>此处写研究的局限性</limitation>
<future>此处写延伸方向与改进方案</future>"""

        try:
            self.safe_log(f"启动推理引擎 [{target_model}] ...")
            response = client.chat.completions.create(
                model=target_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": text[:80000]}
                ],
                temperature=0.3,
                stream=True  
            )
            
            result_text = ""
            is_first_token = True
            
            for chunk in response:
                if is_first_token:
                    self.safe_log("建立数据连接，开始接收解析流...")
                    self.safe_stream_append("\n> 模型流式输出启动中...\n\n")
                    is_first_token = False

                if hasattr(chunk, 'choices') and chunk.choices and hasattr(chunk.choices[0], 'delta'):
                    delta = chunk.choices[0].delta
                    reasoning = getattr(delta, 'reasoning_content', None)
                    if reasoning: self.safe_stream_append(reasoning)
                    content = getattr(delta, 'content', None)
                    if content:
                        result_text += content
                        self.safe_stream_append(content)
            
            self.safe_stream_append("\n\n> 本篇输出流结束。\n")
            self.safe_log("数据接收完毕，正在构建结构化视图...")

            def extract_tag(tag, content):
                match = re.search(f'<{tag}>(.*?)(</{tag}>|$)', content, re.DOTALL | re.IGNORECASE)
                return match.group(1).strip() if match else "未提取到该部分内容。"

            if is_review:
                return {
                    "paper_type": "Review",
                    "title_cn": extract_tag("title_cn", result_text),
                    "title_en": extract_tag("title_en", result_text),
                    "journal_info": extract_tag("journal_info", result_text),
                    "authors": extract_tag("authors", result_text),
                    "abstract": extract_tag("abstract", result_text),
                    "core_viewpoints": extract_tag("core_viewpoints", result_text),
                    "section_breakdown": extract_tag("section_breakdown", result_text),
                    "overall_logic": extract_tag("overall_logic", result_text),
                    "conclusion": extract_tag("conclusion", result_text)
                }
            else:
                return {
                    "paper_type": "Research",
                    "title_cn": extract_tag("title_cn", result_text),
                    "title_en": extract_tag("title_en", result_text),
                    "journal_info": extract_tag("journal_info", result_text),
                    "authors": extract_tag("authors", result_text),
                    "abstract": extract_tag("abstract", result_text),
                    "background": extract_tag("background", result_text),
                    "methods": extract_tag("methods", result_text),
                    "results": extract_tag("results", result_text),
                    "discussion": extract_tag("discussion", result_text),
                    "conclusion": extract_tag("conclusion", result_text),
                    "limitation": extract_tag("limitation", result_text),
                    "future": extract_tag("future", result_text)
                }
        except Exception as e:
            self.safe_log(f"❌ 推理异常: {str(e)}")
            return None

    def _calculate_html_base_size(self, data, is_for_wiz=False):
        text_content_size = sum(len(value.encode('utf-8')) for key, value in data.items() if isinstance(value, str))
        css_size = 3500
        js_size = 2500 if not is_for_wiz else 0
        structure_overhead = 2000
        return text_content_size + css_size + js_size + structure_overhead

    def generate_html(self, paper_id, data, figures, is_for_wiz=False):
        paper_type = data.get("paper_type", "Research")
        content_key = 'section_breakdown' if paper_type == "Review" else 'results'
        
        data_copy = data.copy()
        content_with_figs = data_copy.get(content_key, '')
        
        cursor_url = "https://api.cursorai.live/v1/chat/completions"
        cursor_key = "sk-rTAmhMFzZFlVFEAg5Qtddt6IKMICGrkpwfj85l80Mq5Vu6It"
        cursor_model = "gemini-3.1-pro-preview"
        
        temp_data_copy = data.copy()
        temp_data_copy[content_key] = temp_data_copy.get(content_key, '')
        base_size = self._calculate_html_base_size(temp_data_copy, is_for_wiz)
        
        max_total_size = 4.8 * 1024 * 1024
        buffer_size = 100 * 1024
        available_for_images = max(0, max_total_size - base_size - buffer_size)
        total_image_size = sum(len(fig_data.get('data', b'')) for fig_data in figures.values())
        
        if total_image_size > available_for_images and available_for_images > 0:
            compression_ratio = available_for_images / total_image_size
            self.safe_log(f"⚠️ HTML总大小将超过4.8MB限制，图片将额外压缩至 {compression_ratio*100:.0f}% 以控制总大小...")
            for fig_num, fig_data in figures.items():
                if 'original_data' not in fig_data:
                    fig_data['original_data'] = fig_data['data']
                target_size = int(len(fig_data['original_data']) * compression_ratio)
                img_data, img_ext, orig_size, compressed_size, img_quality = self.compress_image_for_html(
                    fig_data['original_data'], source_ext=fig_data.get('ext', 'png'),
                    max_width=800, quality=75, min_quality=50, target_bytes=target_size
                )
                fig_data['data'] = img_data
                fig_data['ext'] = img_ext
                fig_data['compressed_size'] = compressed_size
                fig_data['quality'] = img_quality
                if 'b64' in fig_data: del fig_data['b64']
        
        final_check_size = base_size + sum(len(fig_data.get('data', b'')) for fig_data in figures.values())
        if final_check_size > max_total_size:
            self.safe_log(f"⚠️ 即使压缩后HTML仍超过4.8MB，将跳过部分图片以确保总大小在限制内...")
            sorted_figures = sorted(figures.items(), key=lambda x: len(x[1].get('data', b'')))
            kept_figures = {}
            current_size = base_size
            for fig_num, fig_data in sorted_figures:
                fig_size = len(fig_data.get('data', b''))
                if current_size + fig_size <= max_total_size - buffer_size:
                    kept_figures[fig_num] = fig_data
                    current_size += fig_size
                else:
                    self.safe_log(f"  跳过图 {fig_num} ({fig_size/1024:.1f}KB) 以控制总大小")
            figures = kept_figures
        
        figure_html_blocks = []
        for fig_num, fig_data in figures.items():
            img_ext = fig_data['ext']
            if 'b64' not in fig_data:
                fig_data['b64'] = base64.b64encode(fig_data['data']).decode('ascii')
            b64 = fig_data['b64']
            ai_btn_html = f'<button class="ai-btn copy-ignore" onclick="window.askAboutFigure({fig_num})">追问 AI</button>' if not is_for_wiz else ''
            img_html = f'''
            <div class="figure-box">
                <img src="data:image/{img_ext};base64,{b64}" alt="图 {fig_num}" id="fig-{fig_num}-img" loading="lazy" decoding="async">
                <div class="figure-header">
                    <span class="figure-number">图 {fig_num} (原稿第 {fig_data['page']} 页)</span>
                    {ai_btn_html}
                </div>
            </div>
            '''
            placeholder = f'[图 {fig_num}]'
            if placeholder in content_with_figs:
                content_with_figs = content_with_figs.replace(placeholder, img_html)
            else:
                figure_html_blocks.append(img_html)
        if figure_html_blocks:
            content_with_figs = ''.join([content_with_figs, ''.join(figure_html_blocks)])
        
        data_copy[content_key] = content_with_figs

        def normalize_model_markup(text):
            text = html_lib.unescape(str(text))
            text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
            text = re.sub(r'</p>\s*<p[^>]*>', '\n\n', text, flags=re.IGNORECASE)
            text = re.sub(r'</?(?:p|section|article)[^>]*>', '\n\n', text, flags=re.IGNORECASE)
            text = re.sub(r'<h([1-6])[^>]*>(.*?)</h\1>', lambda m: '\n\n' + '#' * max(1, min(int(m.group(1)) - 1, 5)) + ' ' + re.sub(r'<[^>]+>', '', m.group(2)).strip() + '\n\n', text, flags=re.IGNORECASE | re.DOTALL)
            text = re.sub(r'<li[^>]*>(.*?)</li>', lambda m: '\n- ' + re.sub(r'<[^>]+>', '', m.group(1)).strip(), text, flags=re.IGNORECASE | re.DOTALL)
            text = re.sub(r'</?(?:ul|ol)[^>]*>', '\n\n', text, flags=re.IGNORECASE)
            text = re.sub(r'<strong[^>]*>(.*?)</strong>', r'**\1**', text, flags=re.IGNORECASE | re.DOTALL)
            text = re.sub(r'<b[^>]*>(.*?)</b>', r'**\1**', text, flags=re.IGNORECASE | re.DOTALL)
            text = re.sub(r'<em[^>]*>(.*?)</em>', r'*\1*', text, flags=re.IGNORECASE | re.DOTALL)
            text = re.sub(r'<i[^>]*>(.*?)</i>', r'*\1*', text, flags=re.IGNORECASE | re.DOTALL)
            return text

        def normalize_list_markers(text):
            lines = text.split('\n')
            normalized = []
            bullet_chars = '•·●◦▪■□◆◇※＊—–'
            for line in lines:
                stripped = line.lstrip()
                indent = line[:len(line) - len(stripped)]
                if stripped and stripped[0] in bullet_chars:
                    rest = stripped[1:].lstrip()
                    normalized.append(f"{indent}- {rest}")
                    continue
                m2 = re.match(r'^([\-\*\+])([^\s\-\*\+].*)$', stripped)
                if m2:
                    normalized.append(f"{indent}{m2.group(1)} {m2.group(2)}")
                    continue
                normalized.append(line)
            text = '\n'.join(normalized)
            out_lines = text.split('\n')
            result = []
            list_re = re.compile(r'^[ \t]*(?:[-\*\+]|\d+\.)\s+')
            prev_is_list = False
            for idx, ln in enumerate(out_lines):
                is_list = bool(list_re.match(ln))
                if is_list and not prev_is_list and result and result[-1].strip() != '':
                    result.append('')
                result.append(ln)
                prev_is_list = is_list
            return '\n'.join(result)

        def format_text(text):
            if not text: return ""
            text = normalize_model_markup(text).replace('\r\n', '\n').replace('\r', '\n')
            raw_blocks = {}
            def stash_raw(match):
                key = f"@@RAW_BLOCK_{len(raw_blocks)}@@"
                raw_blocks[key] = match.group(0)
                return f"\n\n{key}\n\n"
            text = re.sub(r'<div class="figure-box">.*?</div>\s*</div>', stash_raw, text, flags=re.DOTALL)
            text = re.sub(r'<div class="table-wrapper"><table>.*?</table></div>', stash_raw, text, flags=re.DOTALL)
            text = normalize_list_markers(text)
            output = markdown.markdown(text, extensions=["extra", "tables", "nl2br", "sane_lists"], output_format="html5")
            output = re.sub(r'(?<!<div class="table-wrapper">)(<table>.*?</table>)', r'<div class="table-wrapper">\1</div>', output, flags=re.DOTALL)
            for key, value in raw_blocks.items():
                output = output.replace(f"<p>{key}</p>", value)
                output = output.replace(key, value)
            return output

        script_imports = '<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>' if not is_for_wiz else ''
        copy_btn_html = '<button id="copy-article-btn" class="main-copy-btn copy-ignore" onclick="window.copyArticleText()">📋复制</button>' if not is_for_wiz else ''
        favicon_html = '''<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23e11a27' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><path d='M22 10v6M2 10l10-5 10 5-10 5z'/><path d='M6 12v5c3 3 9 3 12 0v-5'/></svg>" type="image/svg+xml">'''
        body_style = "padding: 10px; background: #ffffff;" if is_for_wiz else "padding: 40px 20px; background: var(--bg-page);"
        container_style = "max-width: 100%; margin: 0; padding: 15px; border-radius: 0; box-shadow: none; margin-bottom: 20px;" if is_for_wiz else "max-width: 820px; margin: 0 auto; padding: 60px 70px; border-radius: 12px; box-shadow: 0 4px 24px rgba(0,0,0,0.04); margin-bottom: 100px;"
        
        chat_and_js_html = ""
        if not is_for_wiz:
            chat_and_js_html = f'''
<div id="ai-chat-widget" class="copy-ignore">
    <div class="chat-header" onclick="window.toggleChat()">
        <span>✨Gemini-3.1-pro</span>
        <span id="chat-toggle-icon" style="color: #6b7280; font-size: 12px;">▲</span>
    </div>
    <div class="chat-body" id="chat-body"><div class="msg ai">你好！关于这篇文献的任何细节，或者想深度解读某张特定图表，随时告诉我。</div></div>
    <div class="chat-input-area">
        <input type="text" id="chat-input" placeholder="输入你想追问的问题..." onkeypress="window.handleKeyPress(event)">
        <button class="send-btn" onclick="window.sendMessage()">发送</button>
    </div>
</div>
<script>
const LLM_API_URL = "{cursor_url}"; 
const LLM_API_KEY = "{cursor_key}"; 
const LLM_MODEL = "{cursor_model}";
window.currentContextImage = null;
window.copyArticleText = async function() {{
    const btn = document.getElementById('copy-article-btn');
    const originalText = btn.innerHTML;
    btn.innerHTML = '⏳ 正在处理...';
    try {{
        const selection = window.getSelection(); 
        const range = document.createRange();
        range.selectNodeContents(document.getElementById('content-container'));
        selection.removeAllRanges(); 
        selection.addRange(range);
        document.execCommand('copy'); 
        btn.innerHTML = '✅ 复制成功';
    }} catch (e) {{ alert('请手动全选复制'); }}
    setTimeout(() => {{ btn.innerHTML = originalText; }}, 2000);
}};
window.isChatOpen = false;
window.toggleChat = function() {{
    const widget = document.getElementById('ai-chat-widget');
    window.isChatOpen = !window.isChatOpen;
    if (window.isChatOpen) {{ widget.classList.add('open'); document.getElementById('chat-input').focus(); 
    }} else {{ widget.classList.remove('open'); }}
}};
window.appendMsg = function(sender, text, isImage = false) {{
    const chatBody = document.getElementById('chat-body');
    const div = document.createElement('div'); 
    div.className = 'msg ' + sender;
    div.innerHTML = isImage ? text + ' <span class="vision-badge">📸 已附加当前图表视觉上下文</span>' : text;
    chatBody.appendChild(div); 
    chatBody.scrollTop = chatBody.scrollHeight; 
    return div;
}};
window.handleKeyPress = function(e) {{ if (e.key === 'Enter') window.sendMessage(); }};
window.askAboutFigure = function(figNum) {{
    if (!window.isChatOpen) window.toggleChat();
    const imgElement = document.getElementById('fig-' + figNum + '-img');
    if (imgElement && imgElement.src) {{ 
        window.currentContextImage = imgElement.src; 
        window.sendMessage('请仔细观察并深度解读一下这幅图表中的数据以及说明的问题。', true); 
    }} else {{ window.sendMessage('请详细解读一下图 ' + figNum + ' 的数据与机制。'); }}
}};
window.sendMessage = async function(overrideText = null, showVisionBadge = false) {{ 
    const input = document.getElementById('chat-input');
    const text = overrideText || input.value.trim();
    if (!text) return;
    if (!overrideText) input.value = '';
    window.appendMsg('user', text, showVisionBadge);
    const loadingMsg = window.appendMsg('ai', '思考中...');
    try {{
        const container = document.getElementById('content-container');
        const articleContext = container ? container.innerText : "";
        const sysMsg = {{ role: "system", content: "你是一个专业的论文解答助手。以下是用户正在阅读的论文分析报告的全文内容，请结合这些内容回答用户的问题：\\n\\n" + articleContext.substring(0, 8000) }};
        let messages = [];
        if (window.currentContextImage) {{
            messages = [sysMsg, {{ role: "user", content: [ {{ type: "text", text: text }}, {{ type: "image_url", image_url: {{ url: window.currentContextImage }} }} ] }}];
            window.currentContextImage = null; 
        }} else {{
            messages = [sysMsg, {{ role: "user", content: text }}];
        }}
        const response = await fetch(LLM_API_URL, {{ method: 'POST', headers: {{'Content-Type': 'application/json', 'Authorization': 'Bearer ' + LLM_API_KEY}}, body: JSON.stringify({{ model: LLM_MODEL, messages: messages, stream: false }}) }});
        if (!response.ok) {{ const errData = await response.json().catch(() => ({{}})); throw new Error(`网络错误 [${{response.status}}]: ${{errData.error?.message || '未知错误'}}`); }}
        const data = await response.json();
        const aiReply = data.choices[0].message.content;
        loadingMsg.innerHTML = window.marked ? marked.parse(aiReply) : aiReply.replace(/\\n/g, '<br>');
        const chatBody = document.getElementById('chat-body');
        chatBody.scrollTop = chatBody.scrollHeight;
    }} catch (e) {{ loadingMsg.innerHTML = "❌ AI 响应失败: " + e.message; }}
}};
</script>'''

        html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{data.get('title_cn', '文献分析报告')}</title>
{favicon_html}
{script_imports}
<style>
:root {{ --text-main: #1f2937; --text-muted: #6b7280; --bg: #ffffff; --bg-page: #f9fafb; --accent: #e11a27; --border: #e5e7eb; }}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, "Microsoft YaHei", "Segoe UI", Roboto, sans-serif; line-height: 1.65; color: var(--text-main); {body_style} }}
.container {{ position: relative; background: var(--bg); {container_style} }}
h1 {{ font-family: "Georgia", "Microsoft YaHei", serif; font-size: 28px; font-weight: 700; color: #111111; margin-bottom: 12px; line-height: 1.3; }}
.title-en {{ font-size: 16px; color: var(--text-muted); margin-bottom: 35px; line-height: 1.4; }}
.meta-card {{ font-size: 14px; border-top: 2px solid #111111; border-bottom: 1px solid var(--border); padding: 15px 0 25px; margin-bottom: 40px; color: #4b5563; }}
.meta-card div {{ margin-bottom: 6px; display: flex; align-items: flex-start; }}
.meta-card strong {{ font-weight: 600; white-space: nowrap; margin-right: 15px; min-width: 50px; color: #111111; }}
h2 {{ font-family: "Georgia", "Microsoft YaHei", serif; font-size: 20px; color: #e11a27; margin: 45px 0 15px; font-weight: bold; border-bottom: 1px solid #eeeeee; padding-bottom: 5px; }}
h3 {{ font-size: 18px; color: #111111; margin: 28px 0 12px; font-weight: 700; }}
h4, h5, h6 {{ font-size: 16px; color: #374151; margin: 22px 0 10px; font-weight: 700; }}
p {{ margin-bottom: 16px; text-align: justify; font-size: 16px; color: #374151; }}
ul, ol {{ margin: 0 0 16px 24px; color: #374151; }}
li {{ margin-bottom: 8px; font-size: 16px; }}
code {{ background: #f3f4f6; padding: 2px 5px; border-radius: 4px; font-family: Consolas, monospace; font-size: 0.92em; }}
.table-wrapper {{ overflow-x: auto; margin: 20px 0; }}
table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
th, td {{ border: 1px solid var(--border); padding: 8px 10px; text-align: left; vertical-align: top; }}
th {{ background: #f9fafb; font-weight: 700; color: #111111; }}
strong {{ color: #111111; font-weight: 600; }}
.academic-icon {{ width: 30px; height: 30px; vertical-align: middle; margin-right: 12px; position: relative; top: -3px; stroke: var(--accent); }}
.main-copy-btn {{ display: inline-flex; align-items: center; vertical-align: middle; margin-left: 12px; background: #f3f4f6; border: none; padding: 5px 12px; font-size: 13px; font-weight: 500; font-family: -apple-system, "Microsoft YaHei", sans-serif; border-radius: 4px; cursor: pointer; color: #4b5563; position: relative; top: -2px; transition: background 0.2s; }}
.main-copy-btn:hover {{ background: #e5e7eb; }}
.abstract-box p {{ font-weight: 500; font-size: 17px; line-height: 1.7; color: #111111; }}
.figure-box {{ margin: 40px 0; padding: 20px; border-radius: 8px; border: 1px solid var(--border); text-align: center; background: #fff; }}
.figure-box img {{ max-width: 100%; height: auto; border-radius: 4px; margin-bottom: 15px; }}
.figure-header {{ display: flex; justify-content: space-between; align-items: center; padding-top: 10px; border-top: 1px solid var(--border); }}
.figure-number {{ font-size: 14px; font-weight: bold; color: #111111; }}
.ai-btn {{ background: var(--accent); border: none; color: #fff; border-radius: 4px; padding: 6px 14px; font-size: 12px; cursor: pointer; }}
#ai-chat-widget {{ position: fixed; bottom: 20px; right: 20px; width: 380px; background: #fff; border: 1px solid var(--border); box-shadow: 0 10px 40px rgba(0,0,0,0.1); display: flex; flex-direction: column; transform: translateY(calc(100% - 48px)); transition: transform 0.3s; z-index: 9999; border-radius: 8px; overflow: hidden; }}
#ai-chat-widget.open {{ transform: translateY(0); }}
.chat-header {{ background: #111111; color: #ffffff; padding: 14px 20px; font-weight: 600; cursor: pointer; display: flex; justify-content: space-between;}}
.chat-body {{ height: 420px; padding: 20px; overflow-y: auto; background: #f9fafb; display: flex; flex-direction: column; gap: 10px;}}
.msg {{ max-width: 85%; padding: 10px 14px; font-size: 13px; border-radius: 8px; line-height: 1.5; }}
.msg.ai {{ background: #fff; border: 1px solid var(--border); color: #111; align-self: flex-start; }}
.msg.user {{ background: var(--accent); color: #fff; align-self: flex-end; }}
.vision-badge {{ display: block; font-size: 11px; margin-top: 5px; color: #ffeb3b; opacity: 0.9; }}
.chat-input-area {{ padding: 15px; background: #fff; border-top: 1px solid var(--border); display: flex; gap: 10px; }}
#chat-input {{ flex: 1; padding: 10px; border: 1px solid var(--border); border-radius: 4px; outline: none; }}
.send-btn {{ background: #111111; color: #fff; border: none; padding: 0 16px; border-radius: 4px; cursor: pointer; }}
@media (max-width: 768px) {{
    body {{ padding: 0 !important; }}
    .container {{ padding: 15px !important; margin-bottom: 20px !important; border-radius: 0 !important; box-shadow: none !important; }}
    h1 {{ font-size: 22px; }}
    .figure-box {{ padding: 10px !important; margin: 20px 0 !important; }}
    #ai-chat-widget {{ width: 100% !important; right: 0 !important; bottom: 0 !important; border-radius: 12px 12px 0 0 !important; }}
}}
</style>
</head>
<body>
<div class="container" id="content-container">
    <h1>
        <svg class="academic-icon" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M22 10v6M2 10l10-5 10 5-10 5z" />
            <path d="M6 12v5c3 3 9 3 12 0v-5" />
        </svg>
        {data_copy.get('title_cn', '未提取到中文标题')} {copy_btn_html}
    </h1>
    <div class="title-en">{data_copy.get('title_en', '')}</div>
    <div class="meta-card">
        <div><strong>期刊信息</strong> <span>{format_text(data_copy.get('journal_info', ''))}</span></div>
        <div><strong>作者团队</strong> <span>{format_text(data_copy.get('authors', ''))}</span></div>
    </div>
    <div class="content-section abstract-box">{format_text(data_copy.get('abstract', ''))}</div>'''

        html_parts = [html]
        if paper_type == "Review":
            html_parts.append(f'''
    <h2>核心观点与意义 (Core Viewpoints)</h2><div class="content-section">{format_text(data_copy.get('core_viewpoints', ''))}</div>
    <h2>逐节深度解读 (Section Breakdown)</h2><div class="content-section">{format_text(data_copy.get('section_breakdown', ''))}</div>
    <h2>整体逻辑架构 (Overall Logic)</h2><div class="content-section">{format_text(data_copy.get('overall_logic', ''))}</div>
    <h2>总结与展望 (Conclusion & Future)</h2><div class="content-section">{format_text(data_copy.get('conclusion', ''))}</div>
''')
        else:
            html_parts.append(f'''
    <h2>研究背景 (Background)</h2><div class="content-section">{format_text(data_copy.get('background', ''))}</div>
    <h2>实验方法 (Methods)</h2><div class="content-section">{format_text(data_copy.get('methods', ''))}</div>
    <h2>核心成果 (Results)</h2><div class="content-section">{format_text(data_copy.get('results', ''))}</div>
    <h2>探讨分析 (Discussion)</h2><div class="content-section">{format_text(data_copy.get('discussion', ''))}</div>
    <h2>归纳总结 (Conclusion)</h2><div class="content-section">{format_text(data_copy.get('conclusion', ''))}</div>
    <h2>局限与未来 (Limitations & Future)</h2><div class="content-section">
        <p><strong>局限：</strong></p>{format_text(data_copy.get('limitation', ''))}
        <p><strong>展望：</strong></p>{format_text(data_copy.get('future', ''))}
    </div>
''')

        html_parts.append(f'''
</div>
{chat_and_js_html}
</body>
</html>''')
        return ''.join(html_parts)

    def build_upload_body(self, filename, encoded_content):
        boundary = f"----PaperAnalyzer{int(time.time() * 1000)}"
        safe_filename = filename.replace('\\', '_').replace('"', '_').replace('\r', '_').replace('\n', '_')
        head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{safe_filename}\"\r\nContent-Type: text/html; charset=utf-8\r\n\r\n").encode('utf-8')
        tail = f"\r\n--{boundary}--\r\n".encode('utf-8')
        parts = [head, encoded_content, tail]
        return boundary, parts, sum(len(part) for part in parts)

    def upload_html(self, html_content, filename):
        self.safe_log(f"正在上传云端报告: {filename} ...")
        encoded_content = html_content.encode('utf-8')
        upload_size_mb = len(encoded_content) / 1024 / 1024
        for attempt in range(NETWORK_RETRY_TIMES + 1):
            try:
                self.safe_log(f"云端报告大小: {upload_size_mb:.2f} MB，正在提交第 {attempt + 1} 次请求...")
                self.set_progress(0)
                last_percent = {'value': -1}
                def on_upload_progress(sent, total):
                    percent = int(sent * 100 / total) if total else 0
                    self.set_progress(sent / total if total else 0)
                    if percent >= last_percent['value'] + 10 or percent == 100:
                        last_percent['value'] = percent
                        self.safe_log(f"云端上传进度：{percent}%")
                boundary, upload_parts, upload_total = self.build_upload_body(filename, encoded_content)
                body_stream = MultipartUploadStream(upload_parts, on_upload_progress)
                headers = {"Content-Type": f"multipart/form-data; boundary={boundary}", "Content-Length": str(upload_total)}
                r = requests.post(UPLOAD_URL, data=body_stream, headers=headers, timeout=(15, 180))
                self.set_progress(1)
                r.raise_for_status()
                result = r.json()
                if not result.get("success"): raise RuntimeError(result.get("error", "上传接口返回失败"))
                self.safe_log(f"✅ 云端报告生成成功。")
                return True
            except Exception as e:
                if attempt < NETWORK_RETRY_TIMES:
                    self.safe_log(f"⚠️ 云端上传异常，{NETWORK_RETRY_INTERVAL} 秒后重试第 {attempt + 1} 次: {str(e)}")
                    time.sleep(NETWORK_RETRY_INTERVAL)
                else:
                    self.safe_log(f"⚠️ 云端上传最终失败，将仅保存在本地: {str(e)}")
                    return False

    def upload_to_wiznote(self, html_content, title):
        self.safe_log("正在同步至为知笔记知识库...")
        wiz_size_mb = len(html_content.encode('utf-8')) / 1024 / 1024
        for attempt in range(NETWORK_RETRY_TIMES + 1):
            try:
                self.safe_log(f"为知笔记内容大小：{wiz_size_mb:.2f} MB，正在登录第 {attempt + 1} 次请求...")
                self.set_progress(0.1)
                session = requests.Session()
                login_resp = session.post(f"{WIZ_URL}/as/user/login", json={"userId": WIZ_USER, "password": WIZ_PASS}, timeout=15)
                login_resp.raise_for_status()
                login_data = login_resp.json()
                if login_data.get("returnCode") != 200:
                    if attempt < NETWORK_RETRY_TIMES:
                        self.safe_log(f"❌ 笔记登录失败，{NETWORK_RETRY_INTERVAL} 秒后重试: {login_data.get('returnMessage')}")
                        time.sleep(NETWORK_RETRY_INTERVAL)
                        continue
                    return False
                self.set_progress(0.3)
                self.safe_log("为知笔记登录成功，正在提交笔记内容...")
                token = login_data.get("result", {}).get("token")
                kb_guid = "00000000-0000-0000-0000-000000000000"
                headers = {"X-Wiz-Token": token}
                note_payload = {"kbGuid": kb_guid, "title": title, "category": "/文章笔记/", "html": html_content}
                self.set_progress(0.65)
                note_resp = session.post(f"{WIZ_URL}/ks/note/create/{kb_guid}", headers=headers, json=note_payload, timeout=60)
                note_resp.raise_for_status()
                note_data = note_resp.json()
                if note_data.get("returnCode") == 200:
                    self.set_progress(1)
                    self.safe_log("✅ 成功保存至为知笔记！")
                    return True
                if attempt < NETWORK_RETRY_TIMES:
                    self.safe_log(f"❌ 笔记创建失败，重试中: {note_data.get('returnMessage')}")
                    time.sleep(NETWORK_RETRY_INTERVAL)
                else:
                    return False
            except Exception as e:
                if isinstance(e, requests.HTTPError) and e.response is not None and e.response.status_code == 413:
                    self.safe_log("❌ 同步笔记失败：内容体积过大，服务器拒绝接收。")
                    return False
                if attempt < NETWORK_RETRY_TIMES:
                    time.sleep(NETWORK_RETRY_INTERVAL)
                else:
                    return False

    def process_single_paper(self, file_path):
        ext = os.path.splitext(file_path)[1].lower()
        if ext == '.epub': text, figures = self.extract_epub_data(file_path)
        else: text, figures = self.extract_pdf_data(file_path)
            
        if not text.strip():
            self.safe_log("❌ 从该文件提取文本失败。")
            return

        llm_result = self.call_llm_api(text)
        if not llm_result:
            self.safe_log("❌ LLM 推理失败，跳过后续步骤。")
            return

        current_count = self.config.get("paper_counter", 762)
        safe_title = re.sub(r'[\\/*?:"<>|]', "", llm_result.get('title_cn', '解读报告')).strip()
        formatted_title = f"{current_count:03d}-{safe_title}"
        llm_result['title_cn'] = formatted_title
        filename = f"{formatted_title}.html"
        
        self.safe_log("正在组合最终 HTML 报告并嵌入压缩后的图表...")
        html_start_time = time.time()
        html_content_web = self.generate_html("", llm_result, figures, is_for_wiz=False)
        html_content_wiz = self.generate_html("", llm_result, figures, is_for_wiz=True)
        self.safe_log(f"HTML 组合完成，用时 {time.time() - html_start_time:.1f} 秒。")

        is_uploaded = self.upload_html(html_content_web, filename)
        is_wiz_uploaded = self.upload_to_wiznote(html_content_wiz, formatted_title)
        
        # 核心逻辑：仅当云端上传失败时，才在本地生成文件
        if is_uploaded:
            remote_url = f"http://pi.3body.top/paper/papers/{quote(filename)}"
            self.safe_log(f"✅ 云端上传成功，远程地址: {remote_url}")
        else:
            output_dir = "/app/output"
            os.makedirs(output_dir, exist_ok=True)
            save_path = os.path.join(output_dir, filename)
            with open(save_path, "w", encoding="utf-8") as f:
                f.write(html_content_web)
            self.safe_log(f"❌ 云端上传失败，已生成本地报告: {save_path}")

        self.safe_log(f"📌 上传结果汇总：云端报告{'成功' if is_uploaded else '失败'}；为知笔记{'成功' if is_wiz_uploaded else '失败'}。")
        return current_count - 1

# ==========================================
# Streamlit Web UI
# ==========================================
st.set_page_config(page_title="文献AI解析 v1.3.0", page_icon="📄", layout="wide")

def load_config():
    if os.path.exists(CONFIG_FILE) and os.path.isfile(CONFIG_FILE):
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            config = json.load(f)
    else:
        config = {"paper_counter": 762}
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f)
    if "paper_counter" not in config:
        config["paper_counter"] = 762
    return config

def save_config(config):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

def main():
    if 'config' not in st.session_state:
        st.session_state.config = load_config()
    
    config = st.session_state.config

    with st.sidebar:
        st.title("📄 AI论文解读")
        st.markdown("---")
        
        provider_names = [data["name"] for data in API_REGISTRY.values()]
        selected_provider = st.selectbox("API服务商", provider_names, index=provider_names.index(config.get("provider", provider_names[0])) if config.get("provider") in provider_names else 0)
        
        models = []
        for data in API_REGISTRY.values():
            if data["name"] == selected_provider:
                models = data["models"]
                break
        
        selected_model = st.selectbox("推理模型", models, index=models.index(config.get("model", models[0])) if config.get("model") in models else 0)
        
        st.number_input("当前文献编号", value=config.get("paper_counter", 762), disabled=True, key="ui_counter")
        
        if st.button("🔄 从为知笔记同步编号", use_container_width=True):
            engine_temp = PaperAnalyzerEngine(config, None, None, None)
            max_num = engine_temp.fetch_wiznote_max_number()
            if max_num:
                config["paper_counter"] = max_num
                save_config(config)
                st.success(f"同步成功: {max_num}")
                st.rerun()
            else:
                st.warning("同步失败或未找到笔记")

        if st.button("💾 保存设置", use_container_width=True):
            config["provider"] = selected_provider
            config["model"] = selected_model
            save_config(config)
            st.success("设置已保存")

    st.header("文献批量解析引擎")
    uploaded_files = st.file_uploader("支持拖拽：将一个或多个文献 (PDF / EPUB) 拖拽至此", type=['pdf', 'epub'], accept_multiple_files=True)

    if uploaded_files and st.button("🚀 开始解析", type="primary"):
        progress_bar = st.progress(0)
        log_area = st.empty()
        stream_area = st.empty()
        
        logs = []
        stream_text = ""

        def log_callback(text):
            logs.append(text)
            log_area.code("\n".join(logs))

        def stream_callback(text):
            nonlocal stream_text
            stream_text += text
            stream_area.markdown(stream_text + "▌")

        def progress_callback(value):
            progress_bar.progress(value)

        engine = PaperAnalyzerEngine(config, log_callback, stream_callback, progress_callback)

        for idx, uploaded_file in enumerate(uploaded_files):
            log_callback(f"\n=== 正在解析 ({idx+1}/{len(uploaded_files)}): {uploaded_file.name} ===")
            stream_text = "" 
            
            temp_path = f"/tmp/{uploaded_file.name}"
            with open(temp_path, "wb") as f:
                f.write(uploaded_file.getbuffer())
            
            try:
                new_counter = engine.process_single_paper(temp_path)
                if new_counter:
                    config["paper_counter"] = new_counter
                    save_config(config)
            except Exception as e:
                log_callback(f"❌ 解析严重错误: {str(e)}")
            finally:
                if os.path.exists(temp_path):
                    os.remove(temp_path)

            progress_bar.progress((idx + 1) / len(uploaded_files))
            stream_area.empty() 
        
        log_callback("\n🎉 所有文献已处理完毕！")
        st.session_state.config = config 

if __name__ == "__main__":
    main()
