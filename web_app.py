import os
import json
import time
import requests
from datetime import datetime
from urllib.parse import quote
import streamlit as st
import fitz  # PyMuPDF
from PIL import Image
from openai import OpenAI

# ==========================================
# 1. 将你原脚本中的核心类与函数直接粘贴到这里
# 包括：API_REGISTRY, UPLOAD_URL, WIZ_URL 等常量
# 以及：compress_image_for_html, extract_pdf_data, extract_epub_data, 
# call_llm_api, _calculate_html_base_size, _generate_html_base, 
# generate_html, build_upload_body, upload_html, upload_to_wiznote
# ==========================================

# 页面基础配置：默认使用宽屏布局，自动适配窄屏设备，消除过大边距
st.set_page_config(page_title="文献AI解析 v1.3.0", page_icon="📄", layout="wide")

CONFIG_FILE = "analyzer_config.json"

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            config = json.load(f)
    else:
        config = {}
    if "paper_counter" not in config:
        config["paper_counter"] = 762  # 记住现在的编号，默认 762
    return config

def save_config(config):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

def main():
    config = load_config()

    # 侧边栏配置区
    with st.sidebar:
        st.title("📄 AI论文解读")
        st.markdown("---")
        
        provider_names = [data["name"] for data in API_REGISTRY.values()]
        selected_provider = st.selectbox("API服务商", provider_names, index=provider_names.index(config.get("provider", provider_names[0])) if config.get("provider") in provider_names else 0)
        
        # 动态更新模型列表
        models = []
        for data in API_REGISTRY.values():
            if data["name"] == selected_provider:
                models = data["models"]
                break
        
        selected_model = st.selectbox("推理模型", models, index=models.index(config.get("model", models[0])) if config.get("model") in models else 0)
        
        paper_counter = st.number_input("当前文献编号", value=config.get("paper_counter", 762), disabled=True)
        
        if st.button("保存设置", use_container_width=True):
            config["provider"] = selected_provider
            config["model"] = selected_model
            save_config(config)
            st.success("设置已保存")

    # 主工作区
    st.header("文献批量解析")
    uploaded_files = st.file_uploader("将一个或多个文献 (PDF / EPUB) 拖拽至此", type=['pdf', 'epub'], accept_multiple_files=True)

    if uploaded_files and st.button("开始解析", type="primary"):
        progress_bar = st.progress(0)
        log_area = st.empty()
        logs = []

        def safe_log(text):
            logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] {text}")
            log_area.code("\n".join(logs))

        for idx, uploaded_file in enumerate(uploaded_files):
            safe_log(f"\n=== 正在解析 ({idx+1}/{len(uploaded_files)}): {uploaded_file.name} ===")
            
            # 将上传的文件保存为临时文件供解析函数使用
            temp_path = f"/tmp/{uploaded_file.name}"
            with open(temp_path, "wb") as f:
                f.write(uploaded_file.getbuffer())
            
            try:
                # ==========================================
                # 2. 在这里调用你的核心解析逻辑
                # 例如: text, figures = extract_pdf_data(temp_path) (如果是PDF)
                # llm_result = call_llm_api(text)
                # html_content_web = generate_html(...)
                # ==========================================
                
                # 模拟逻辑执行与编号递减
                current_count = config.get("paper_counter", 762)
                safe_title = "解析报告" # 需从 llm_result 获取
                formatted_title = f"{current_count:03d}-{safe_title}"
                filename = f"{formatted_title}.html"
                
                # 上传逻辑：仅当无法上传到服务器时才在本地生成解析后的文件
                is_uploaded = False # 替换为实际上传逻辑: upload_html(html_content_web, filename)
                
                if is_uploaded:
                    safe_log(f"✅ 云端上传成功: {filename}")
                else:
                    save_path = os.path.join("/app/output", filename)
                    # 写入本地文件...
                    safe_log(f"❌ 云端上传失败，已在本地生成解析文件: {save_path}")

                # 编号减 1 并保存
                config["paper_counter"] = current_count - 1
                save_config(config)

            except Exception as e:
                safe_log(f"❌ 解析该文件时出错跳过: {str(e)}")
            finally:
                if os.path.exists(temp_path):
                    os.remove(temp_path)

            progress_bar.progress((idx + 1) / len(uploaded_files))
        
        safe_log("\n🎉 所有文献已解析完毕！")

if __name__ == "__main__":
    main()
