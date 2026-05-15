FROM python:3.10-slim

# 设置工作目录
WORKDIR /app

# 安装必要的系统字体和依赖包 (防止 PyMuPDF 在处理某些 PDF 时缺少字体报错)
RUN apt-get update && apt-get install -y \
    fonts-liberation \
    fontconfig \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖文件并安装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目代码
COPY . .

# 创建一个用于保存本地输出的文件夹（仅在服务器上传失败时使用）
RUN mkdir -p /app/output

# 暴露 Streamlit 的默认端口
EXPOSE 8501

# 启动命令
CMD ["streamlit", "run", "web_app.py", "--server.port=8501", "--server.address=0.0.0.0"]
