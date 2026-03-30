# ABCard

ABCard Helper

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
sudo apt-get install -y xvfb 
```

### 2. 安装浏览器

```bash
playwright install chromium
```

### 3. 启动 Web UI

# 启动 UI
streamlit run ui.py --server.address 0.0.0.0 --server.port 8503
```

浏览器访问 `http://localhost:8503`。

### 4. 开发者模式

显示日志、Stripe 响应、高级配置：

```bash
streamlit run ui.py --server.port 8503 -- --dev
```

## 部署

```bash
# 使用自动部署脚本 (Ubuntu/Debian)
sudo bash deploy.sh
```

## License

MIT

📩 Disclaimer | 免责声明
本工具仅供学习和研究使用，使用本工具所产生的任何后果由使用者自行承担。
This tool is only for learning and research purposes, and any consequences arising from the use of this tool are borne by the user.
