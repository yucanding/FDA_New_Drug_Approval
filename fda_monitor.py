import requests
import yfinance as yf
from bs4 import BeautifulSoup
import time
import re
import io
import os
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
import feedparser
from deep_translator import GoogleTranslator
import pytz

# --- 配置 ---
TG_TOKEN = os.environ.get('TG_TOKEN')
TG_CHAT_ID = os.environ.get('TG_CHAT_ID')
ID_FILE = "last_fda_ids.txt"
LAST_SUCCESS_FILE = "last_success_date_drug.txt"

MAPPING = {
    "prnewswire.com": "PRNewswire",
    "globenewswire.com": "GlobeNewswire",
    "businesswire.com": "BusinessWire",
    "bloomberg.com": "彭博社",
    "reuters.com": "路透社",
    "stocktitan.net": "StockTitan"
}

# 设置美东时区
ET_TZ = pytz.timezone('US/Eastern')

def convert_date_to_chinese(date_str):
    try:
        # 兼容两种格式：详情页的 MM/DD/YYYY 和列表页的 Month DD, YYYY
        if "/" in date_str:
            dt = datetime.strptime(date_str, "%m/%d/%Y")
        else:
            dt = datetime.strptime(date_str, "%B %d, %Y")
        return dt.strftime("%Y年%m月%d日").replace("年0", "年").replace("月0", "月")
    except:
        return date_str

def get_detailed_action_date(appl_no):
    url = f"https://www.accessdata.fda.gov/scripts/cder/daf/index.cfm?event=overview.process&ApplNo={appl_no}"
    try:
        time.sleep(0.5)
        resp = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
        soup = BeautifulSoup(resp.text, 'html.parser')
        target_table = soup.find('table', id='exampleApplOrig')
        if target_table:
            date_td = target_table.find('tbody').find('tr').find('td')
            if date_td: return date_td.get_text(strip=True)
    except: pass
    return None

def get_verified_stock_data(company_name):
    try:
        # 1. 对公司名进行预处理，去除 SAS, AG, LTD 等干扰
        core_name = re.sub(r'(?i)\b(inc|corp|ltd|llc|co|company|plc|gmbh|sas|ag|se|s\.a\.s)\b|\.', '', company_name).strip()
        search = yf.Search(core_name, max_results=5) # 增加搜索范围
        
        if not search.quotes: return None
        
        for quote in search.quotes:
            ticker = quote['symbol']
            
            # 获取 Ticker 对象
            stock = yf.Ticker(ticker)
            info = stock.info
            
            # --- 核心过滤三要素 ---
            # A. 货币校验：必须是美元（精准锁定美股市场）
            currency = info.get('currency', info.get('financialCurrency', ''))
            # B. 行业校验：医疗保健
            sector = info.get('sector', '')
            # C. 交易所校验（可选）：排除常见的非美股后缀
            if "." in ticker and not any(ext in ticker for ext in ["PK", "OB"]): 
                # PK/OB 是美股OTC，如果连点都不要，就保持 if "." in ticker: continue
                continue

            if sector == 'Healthcare' and currency == 'USD':
                # 额外增加一个名字包含检查，防止搜索偏移
                short_name = quote.get('shortname', '').upper()
                if core_name.upper().split()[0] in short_name: 
                    return {
                        "ticker": ticker, 
                        "name": quote.get('shortname', company_name),
                        "price": stock.fast_info.last_price,
                        "market_cap": stock.fast_info.market_cap / 1e9
                    }
        return None
    except Exception as e: 
        print(f"股票检索异常: {e}")
        return None

def investigate_first_announcement(symbol, company_name, start_date_str):
    """
    搜寻最早的一条官宣新闻，并返回推送状态文本
    """
    print(f"🔎 正在核实 ${symbol} 的首发官宣...")
    
    # 计算一个月前的时间基准点
    one_month_ago = datetime.now(timezone.utc) - timedelta(days=30)
    
    start_dt = datetime.strptime(start_date_str, "%m/%d/%Y").replace(tzinfo=timezone.utc)
    found_news = []
    headers = {'User-Agent': 'Mozilla/5.0'}

    try:
        ticker_obj = yf.Ticker(symbol)
        cik = str(ticker_obj.info.get('cik', '')).zfill(10)
        sec_url = f"https://data.sec.gov/rss?cik={cik}&exclude=true&count=10" if cik != "0000000000" else None
    except: sec_url = None

    st_url = f"https://www.stocktitan.net/rss/news/{symbol}"
    site_query = " OR ".join([f"site:{d}" for d in MAPPING.keys() if d != "stocktitan.net"])
    
    # 【修改处 1】：在 Google 搜索词最后加上 " when:1m" 限制近一个月
    g_query = f'({site_query}) ("{symbol}" OR "{company_name}") when:1m'
    g_url = f"https://news.google.com/rss/search?q={urllib.parse.quote(g_query)}&hl=en-US&gl=US&ceid=US:en"

    sources = [("SEC官网", sec_url), ("财经媒体", st_url), ("Google聚合", g_url)]

    for source_label, url in sources:
        if not url: continue
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            if "sec.gov" in url:
                root = ET.fromstring(resp.content)
                ns = {'atom': 'http://www.w3.org/2005/Atom'}
                for entry in root.findall('atom:entry', ns):
                    title = entry.find('atom:title', ns).text
                    link = entry.find('atom:link', ns).attrib.get('href')
                    acc_node = entry.find('.//{*}acceptance-date-time')
                    dt_utc = datetime.fromisoformat(acc_node.text.replace('Z', '+00:00')) if acc_node is not None else datetime.now(timezone.utc)
                    
                    # 【修改处 2】：增加过滤，如果 SEC 发布的公告超过一个月则跳过
                    if dt_utc < one_month_ago: continue
                    
                    if any(k in title.upper() for k in ["FDA", "APPROVE", "APPROVAL"]):
                        found_news.append({"ts_utc": dt_utc, "link": link})
            else:
                feed = feedparser.parse(io.BytesIO(resp.content))
                for entry in feed.entries:
                    pub_ts = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                    
                    # 【修改处 3】：过滤修改，不仅要大于 start_dt，还要在过去一个月内
                    if pub_ts < start_dt or pub_ts < one_month_ago: continue
                    
                    if "FDA" in entry.title.upper() and ("APPROVE" in entry.title.upper() or "APPROVAL" in entry.title.upper()):
                        found_news.append({"ts_utc": pub_ts, "link": entry.link})
        except: continue

    if found_news:
        found_news.sort(key=lambda x: x['ts_utc'])
        first_link = found_news[0]['link']
        return f'已官宣 (<a href="{first_link}">点击查看新闻</a>)'
    else:
        return "暂未找到官宣信息"

def send_tg_message(text):
    if not TG_TOKEN or not TG_CHAT_ID or not text: return
    target_ids = [chat_id.strip() for chat_id in TG_CHAT_ID.split(',') if chat_id.strip()]
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    for chat_id in target_ids:
        try:
            requests.post(url, json={
                "chat_id": chat_id, 
                "text": text, 
                "parse_mode": "HTML", 
                "disable_web_page_preview": True
            }, timeout=10)
        except Exception as e:
            print(f"发送失败: {e}")

def main():
    today_str = datetime.now().strftime("%Y-%m-%d")
    if os.path.exists(LAST_SUCCESS_FILE):
        with open(LAST_SUCCESS_FILE, "r") as f:
            if f.read().strip() == today_str:
                print(f"📌 今日 ({today_str}) 已成功推送，跳过。")
                return

    fda_url = "https://www.accessdata.fda.gov/scripts/cder/daf/index.cfm?event=report.page"
    headers = {'User-Agent': 'Mozilla/5.0'}
    
    if os.path.exists(ID_FILE):
        with open(ID_FILE, "r") as f:
            old_ids = set(f.read().splitlines())
    else:
        old_ids = set()

    try:
        response = requests.get(fda_url, headers=headers, timeout=30)
        soup = BeautifulSoup(response.text, 'html.parser')
        tab_panel = soup.find('div', id='example2-tab2')
        if not tab_panel: return

        date_headers = tab_panel.find_all('h4')
        records_to_send = []
        current_all_ids = list(old_ids)

        for header in date_headers:
            table = header.find_next('table')
            if not table: continue
            
            for row in table.find('tbody').find_all('tr'):
                cols = row.find_all('td')
                if len(cols) < 5: continue
                
                # 1. 提取对应列的值并统一转为大写（防止网页大小写变动导致失效）
                sub_text = cols[3].get_text().upper()    # 对应 Submission
                class_text = cols[5].get_text().upper()  # 对应 Submission Classification
                
                # 2. 定义命中条件
                is_orig = "ORIG-1" in sub_text
                is_efficacy = "EFFICACY" in class_text
                
                # 3. 核心逻辑：如果两个条件都不满足，则跳过（continue）
                if not (is_orig or is_efficacy):
                    continue

                note_suffix = " (Efficacy更新)" if is_efficacy else ""

                full_drug_text = cols[0].get_text(separator="\n", strip=True)
                drug_name_only = full_drug_text.split('\n')[0].strip()
                company = cols[4].get_text(strip=True)
                
                link_tag = cols[0].find('a')
                if link_tag and 'href' in link_tag.attrs:
                    match = re.search(r'ApplNo=(\d+)', link_tag['href'])
                    if match:
                        appl_no = match.group(1)
                        if appl_no not in old_ids:
                            # 深度核查
                            action_date = get_detailed_action_date(appl_no)
                            if not action_date: continue
                            
                            stock = get_verified_stock_data(company)
                            if stock:
                                # 核心：获取官宣状态
                                if len(stock['ticker']) > 4:
                                    print(f"⏩ Ticker {stock['ticker']} 长度为 {len(stock['ticker'])}，超过4个字母，已跳过。")
                                    continue
                                announcement_status = investigate_first_announcement(stock['ticker'], stock['name'], action_date)
                                
                                records_to_send.append({
                                    "date": convert_date_to_chinese(action_date) + note_suffix,
                                    "ticker": stock['ticker'],
                                    "company": company,
                                    "drug": drug_name_only,
                                    "cap": stock['market_cap'],
                                    "price": stock['price'],
                                    "status": announcement_status,
                                    "link": f"https://www.accessdata.fda.gov/scripts/cder/daf/index.cfm?event=overview.process&ApplNo={appl_no}"
                                })
                                current_all_ids.append(appl_no)
                                time.sleep(1)

        if records_to_send:
            final_msg = f"<b>🧬 FDA新药获批更新 ({len(records_to_send)}家上市企业)</b>\n\n"
            msg_blocks = []
            for idx, item in enumerate(records_to_send, 1):
                block = (f"{idx}. 📅日期: {item['date']}\n"
                         f"    🏢公司: ${item['ticker']} ({item['company']})\n"
                         f"    💊药品: {item['drug']}\n"
                         f"    💰市值: ${item['cap']:.2f}B\n"
                         f"    💵股价: ${item['price']:.2f}\n"
                         f"    📢状态: {item['status']}\n" # <--- 新增的状态行
                         f'    🔗<a href="{item["link"]}">点击查看FDA公告</a>')
                msg_blocks.append(block)
            
            final_msg += "\n\n---------------\n\n".join(msg_blocks) + "\n\n#FDA #DrugApproval"
            send_tg_message(final_msg)
            
            with open(LAST_SUCCESS_FILE, "w") as f: f.write(today_str)
            with open(ID_FILE, "w") as f: f.write("\n".join(current_all_ids[-200:]))
        else:
            print("💡 本次扫描没有发现符合条件的新获批。")

    except Exception as e:
        print(f"🛑 运行异常: {e}")

if __name__ == "__main__":
    main()
