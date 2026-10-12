import json
import os
import csv
import json
import math
from datetime import datetime
import requests


def _as_mapping(value):
    return value if isinstance(value, dict) else {}


def _as_records(value):
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _safe_float(value, default=0.0):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default

class StockDataCleaner:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.date = os.path.basename(data_dir)
        self.data = {}
        self.load_data()
    
    def load_data(self):
        """加载所有 JSON 数据"""
        files = {
            'dragon_tiger': 'dragon_tiger_list.json',
            'limit_up': 'limit_up_filter.json',
            'market_sentiment': 'market_sentiment_cycle.json',
            'risk_monitor': 'risk_monitor_list.json',
            'sector_heat': 'sector_heat_stats.json',
            'ladder_detail': 'ladder_hierarchy_detail.json',
            'ladder_trend': 'ladder_trend_summary.json'
        }
        
        for key, filename in files.items():
            file_path = os.path.join(self.data_dir, filename)
            if os.path.exists(file_path):
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        payload = json.load(f)
                except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    print(f"[WARN] 跳过损坏的 {filename}: {exc}")
                    continue
                self.data[key] = _as_mapping(payload)
    
    def generate_market_dashboard(self):
        """生成大盘表 market_dashboard.csv"""
        # 创建日期文件夹（如果不存在）- 使用数据所在的目录作为输出目录
        output_dir = self.data_dir
        
        # 检查market_sentiment数据是否为空
        market_sentiment = _as_mapping(self.data.get('market_sentiment'))
        data_list = _as_records(market_sentiment.get('data'))
        
        if not data_list:
            print(f"[WARN] market_sentiment数据为空，跳过生成market_dashboard.csv")
            return
        
        sentiment_data = data_list[0]
        
        # 炸板率和跌停数
        emotion = _as_mapping(sentiment_data.get('emotionMetrics'))
        broken_ratio = _safe_float(emotion.get('brokenRatio'))
        limit_down_count = _safe_float(emotion.get('limitDownCount'))
        
        # 主线题材（取前两个）
        themes = _as_records(sentiment_data.get('themes'))[:2]
        main_themes = ','.join([theme.get('name', '') for theme in themes])
        
        # 最高板高度
        ladder = _as_mapping(sentiment_data.get('ladder'))
        heights = [int(key) for key in ladder if str(key).isdigit()]
        max_height = max(heights, default=0)
        
        # 写入 CSV
        file_path = os.path.join(output_dir, 'market_dashboard.csv')
        with open(file_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['日期', '炸板率', '跌停数', '主线题材', '最高板高度'])
            writer.writerow([self.date, broken_ratio, limit_down_count, main_themes, max_height])
        
        print(f"[OK] market_dashboard.csv 生成完成 (保存在 {output_dir} 文件夹)")
    
    def generate_stock_feature_matrix(self):
        """生成个股因子表 stock_feature_matrix.csv"""
        # 使用数据所在的目录作为输出目录
        output_dir = self.data_dir
        
        # 检查limit_up数据是否完整
        limit_up_payload = _as_mapping(self.data.get('limit_up'))
        limit_up_data = _as_mapping(limit_up_payload.get('data'))
        if not limit_up_data.get('stocks'):
            print(f"[WARN] limit_up数据不完整，跳过生成stock_feature_matrix.csv")
            return
        
        # 获取龙虎榜数据，用于判断是否有机构买入
        dragon_tiger_data = _as_records(_as_mapping(self.data.get('dragon_tiger')).get('data'))
        # 获取风险监控数据，用于判断是否监管
        risk_monitor_data = _as_records(_as_mapping(self.data.get('risk_monitor')).get('data'))
        # 获取涨停过滤数据，用于主要因子
        limit_up_data = _as_records(limit_up_data.get('stocks'))
        
        # 构建辅助数据结构
        stock_regulation = set()
        for stock in risk_monitor_data:
            # 风险监控数据中的code字段是股票代码
            stock_code = stock.get('code', '')
            if stock_code:
                stock_regulation.add(stock_code)
        
        stock_institution_buy = {}
        for record in dragon_tiger_data:
            stock_code = str(record.get('stockCode') or '')
            lhb_branch = _as_mapping(record.get('lhbBranch'))
            buy_branches = _as_records(lhb_branch.get('buyBranches'))
            has_institution = any(branch.get('branchName') == '机构专用' for branch in buy_branches)
            if has_institution:
                stock_institution_buy[stock_code] = True
        
        # 写入 CSV
        file_path = os.path.join(output_dir, 'stock_feature_matrix.csv')
        with open(file_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['代码', '连板数', '封单额', '换手率', '是否监管', '是否机构买入', '龙头地位标签'])
            
            for stock in limit_up_data:
                stock_code = str(stock.get('code') or '')
                continue_num = stock.get('continue_num', 0)
                order_amount = stock.get('order_amount', 0)
                turnover_rate = stock.get('turnover_rate', 0)
                is_regulation = '是' if stock_code in stock_regulation else '否'
                is_institution_buy = '是' if stock_code in stock_institution_buy else '否'
                
                # 龙头地位标签
                tags = stock.get('tags', [])
                leader_tag = ','.join(tags) if tags else '无'
                
                writer.writerow([stock_code, continue_num, order_amount, turnover_rate, is_regulation, is_institution_buy, leader_tag])
        
        print(f"[OK] stock_feature_matrix.csv 生成完成 (保存在 {output_dir} 文件夹)")
    
    def generate_ai_daily_brief(self):
        """生成 AI 压缩提示词 ai_daily_brief.txt"""
        # 使用数据所在的目录作为输出目录
        output_dir = self.data_dir
        
        # 检查market_sentiment数据是否为空
        market_sentiment = _as_mapping(self.data.get('market_sentiment'))
        data_list = _as_records(market_sentiment.get('data'))
        
        if not data_list:
            print(f"[WARN] market_sentiment数据为空，跳过生成ai_daily_brief.txt")
            return
        
        sentiment_data = data_list[0]
        
        # 检查其他必要数据是否存在
        limit_up_payload = _as_mapping(self.data.get('limit_up'))
        limit_up_container = _as_mapping(limit_up_payload.get('data'))
        if not limit_up_container.get('stocks'):
            print(f"[WARN] limit_up数据不完整，跳过生成ai_daily_brief.txt")
            return
            
        sector_payload = _as_mapping(self.data.get('sector_heat'))
        if not isinstance(sector_payload.get('data'), list):
            print(f"[WARN] sector_heat数据不完整，跳过生成ai_daily_brief.txt")
            return
            
        if 'ladder_detail' not in self.data:
            print(f"[WARN] ladder_detail数据缺失，跳过生成ai_daily_brief.txt")
            return
            
        limit_up_data = _as_records(limit_up_container.get('stocks'))
        sector_heat = _as_records(sector_payload.get('data'))
        ladder_detail = _as_mapping(self.data.get('ladder_detail'))
        market = _as_mapping(sentiment_data.get('marketSentiment'))
        emotion = _as_mapping(sentiment_data.get('emotionMetrics'))
        ladder = _as_mapping(sentiment_data.get('ladder'))
        
        # 筛选高标股（连板数 >= 3）
        high_stocks = [stock for stock in limit_up_data if stock.get('continue_num', 0) >= 3]
        high_stocks.sort(key=lambda x: x.get('continue_num', 0), reverse=True)
        
        # 1. 首板与题材发散
        first_board_stocks = [stock for stock in limit_up_data if stock.get('continue_num', 0) == 1]
        # 构建辅助数据结构
        first_board_by_theme = {} 
        for stock in first_board_stocks:
            theme = stock.get('jiuyangongshe_category_name', '其他')
            if theme not in first_board_by_theme:
                first_board_by_theme[theme] = []
            first_board_by_theme[theme].append(stock.get('name', ''))
        
        # 获取监管股票列表
        risk_monitor_data = _as_records(_as_mapping(self.data.get('risk_monitor')).get('data'))
        regulated_stocks = set()
        for stock in risk_monitor_data:
            stock_code = stock.get('code', '')
            if stock_code:
                regulated_stocks.add(stock_code)
        
        # 2. 涨停梯队完整性
        # 从ladder_detail获取更完整的梯队信息
        ladder_dates = _as_records(ladder_detail.get('dates'))
        if not ladder_dates:
            print(f"[WARN] ladder_detail数据不完整，跳过生成ai_daily_brief.txt")
            return
            
        ladder_boards = _as_records(ladder_dates[0].get('boards'))
        complete_ladder = {}  
        for board in ladder_boards:
            level = board.get('level', '')
            for stock in _as_records(board.get('stocks')):
                tags = stock.get('tags') if isinstance(stock.get('tags'), list) else []
                for tag in tags:
                    if tag not in complete_ladder:
                        complete_ladder[tag] = {}
                    if level not in complete_ladder[tag]:
                        complete_ladder[tag][level] = []
                    complete_ladder[tag][level].append(stock.get('name', ''))
        
        # 3. 炸板股与亏钱效应
        broken_ratio = _safe_float(emotion.get('brokenRatio'))
        broken_count = _safe_float(emotion.get('brokenCount'))
        
        # 4. 核心中军走势
        # 按市值排序，找出每个题材的大市值股票
        market_cap_by_theme = {} 
        for stock in limit_up_data:
            theme = stock.get('jiuyangongshe_category_name', '其他')
            if theme not in market_cap_by_theme:
                market_cap_by_theme[theme] = []
            market_cap_by_theme[theme].append({
                'name': stock.get('name', ''),
                'code': stock.get('code', ''),
                'market_cap': _safe_float(stock.get('total_market_cap')),
                'industry': stock.get('industry', '')
            })
        # 对每个题材按市值排序，处理可能的None值
        for theme in market_cap_by_theme:
            market_cap_by_theme[theme].sort(key=lambda x: x['market_cap'] if x['market_cap'] is not None else 0.0, reverse=True)
        
        # 5. 量能与市场风格
        turnover_data = _as_mapping(sentiment_data.get('turnover'))
        turnover = turnover_data.get('value', '未知')
        
        # 6. 情绪量化指标
        promotion_rates = _as_mapping(emotion.get('promotionRates'))
        
        # 7. 轮动信号
        # 从sector_heat获取板块热度信息
        themes = _as_records(sentiment_data.get('themes'))
        main_themes = [theme.get('name', '') for theme in themes[:2]]
        other_themes = [theme.get('name', '') for theme in themes[2:]]
        
        # 构建 brief 内容
        brief = []
        brief.append(f"日期：{self.date}")
        brief.append(f"市场情绪：上涨家数 {market.get('rise', 0)}，下跌家数 {market.get('fall', 0)}")
        brief.append(f"炸板率：{broken_ratio:.2%}，跌停数：{emotion.get('limitDownCount', 0)}")
        brief.append(f"主线题材：{','.join(main_themes)}")
        heights = [int(key) for key in ladder if str(key).isdigit()]
        brief.append(f"最高板高度：{max(heights, default=0)} 板")
        brief.append(f"两市成交总额：{turnover}")
        brief.append("")
        
        # 1. 首板与题材发散
        brief.append("1. 首板与题材发散：")
        brief.append(f"   首板总数：{len(first_board_stocks)}")
        for theme, stocks in first_board_by_theme.items():
            brief.append(f"   {theme}：{len(stocks)}只")
        # 检查是否有全新题材
        all_themes = [theme.get('name', '') for theme in sector_heat if theme.get('name')]
        new_themes = [theme for theme in all_themes if theme not in ['商业航天', '脑机接口', '化工']]
        if new_themes:
            brief.append(f"   全新题材：{','.join(new_themes)}")
        brief.append("")
        
        # 2. 涨停梯队完整性
        brief.append("2. 涨停梯队完整性：")
        for theme in main_themes:
            if theme in complete_ladder:
                heights = sorted(complete_ladder[theme].keys(), reverse=True)
                brief.append(f"   {theme}梯队：")
                for height in heights:
                    stocks_str = ','.join(complete_ladder[theme][height][:5])  # 只显示前5只
                    if len(complete_ladder[theme][height]) > 5:
                        stocks_str += f"等{len(complete_ladder[theme][height])}只"
                    brief.append(f"     {height}板：{stocks_str}")
        brief.append("")
        
        # 3. 炸板股与亏钱效应
        brief.append("3. 炸板股与亏钱效应：")
        brief.append(f"   炸板总数：{broken_count}")
        brief.append(f"   炸板率：{broken_ratio:.2%}")
        # 计算炸板率高低
        if broken_ratio > 0.2:
            brief.append(f"   炸板率较高，市场分歧明显")
        elif broken_ratio < 0.1:
            brief.append(f"   炸板率较低，市场情绪稳定")
        else:
            brief.append(f"   炸板率适中，市场分歧可控")
        brief.append("")
        
        # 4. 核心中军走势
        brief.append("4. 核心中军走势：")
        for theme in main_themes:
            if theme in market_cap_by_theme and market_cap_by_theme[theme]:
                中军 = market_cap_by_theme[theme][0]
                brief.append(f"   {theme}中军：{中军['name']}（{中军['code']}），市值：{中军['market_cap']:,}元")
        brief.append("")
        
        # 5. 量能与市场风格
        brief.append("5. 量能与市场风格：")
        brief.append(f"   两市成交总额：{turnover}")
        # 判断量能高低
        if turnover_data.get('isHigh'):
            brief.append(f"   量能较高，市场活跃度提升")
        else:
            brief.append(f"   量能适中，市场稳定运行")
        brief.append(f"   （注：缺少连板指数与同花顺全A指数对比数据）")
        brief.append("")
        
        # 6. 情绪量化指标
        brief.append("6. 情绪量化指标：")
        brief.append(f"   1进2晋级率：{promotion_rates.get('1to2', 0)}%")
        brief.append(f"   2进3晋级率：{promotion_rates.get('2to3', 0)}%")
        brief.append(f"   3进4晋级率：{promotion_rates.get('3to4', 0)}%")
        brief.append(f"   高标晋级率：{promotion_rates.get('high', 0)}%")
        # 计算平均晋级率
        avg_promotion = sum(promotion_rates.values()) / len(promotion_rates) if promotion_rates else 0
        if avg_promotion > 80:
            brief.append(f"   平均晋级率较高，情绪加速")
        elif avg_promotion < 30:
            brief.append(f"   平均晋级率较低，情绪降温")
        else:
            brief.append(f"   平均晋级率适中，情绪稳定")
        brief.append("")
        
        # 7. 轮动信号
        brief.append("7. 轮动信号：")
        if other_themes:
            brief.append(f"   其他活跃题材：{','.join(other_themes)}")
        # 从sector_heat获取板块热度变化
        sector_counts = {
            item.get('name', ''): _safe_float(item.get('count'))
            for item in sector_heat
            if item.get('name')
        }
        active_sectors = [name for name, count in sector_counts.items() if count >= 5]
        if active_sectors:
            brief.append(f"   活跃板块：{','.join(active_sectors)}")
        brief.append("")
        
        # 高标股逻辑
        brief.append("8. 高标股逻辑：")
        for stock in high_stocks:
            stock_name = stock.get('name', '')
            stock_code = str(stock.get('code') or '')
            stock_brief = f"   {stock_name}（{stock_code}）：{stock.get('continue_num', 0)}连板，"
            stock_brief += f"涨停类型：{stock.get('limit_up_type', '')}，"
            # 将封单额转换为亿元单位
            order_amount_yuan = _safe_float(stock.get('order_amount'))
            order_amount_yiyuan = order_amount_yuan / 1e8
            stock_brief += f"封单额：{order_amount_yiyuan:.2f}亿元，"
            stock_brief += f"换手率：{_safe_float(stock.get('turnover_rate')):.2f}%，"
            # 使用原始分析内容而非概括
            analysis = str(stock.get('jiuyangongshe_analysis') or stock.get('reason_type') or '')
            # 简化分析内容，只保留第一行核心逻辑
            core_analysis = analysis.split('\n')[0]
            stock_brief += f"概念：{core_analysis}"
            
            # 添加监管标记
            tags = stock.get('tags', [])
            if stock_code in regulated_stocks:
                tags.append("[[WARN]监管]")
            
            if tags:
                stock_brief += f"，标签：{','.join(tags)}"
            
            brief.append(stock_brief)
        
        # 写入文件
        file_path = os.path.join(output_dir, 'ai_daily_brief.txt')
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(brief))
        
        print(f"[OK] ai_daily_brief.txt 生成完成 (保存在 {output_dir} 文件夹)")
    
    def run(self):
        """执行完整的 ETL 流程"""
        print(f"开始处理 {self.date} 的数据...")
        self.generate_market_dashboard()
        self.generate_stock_feature_matrix()
        self.generate_ai_daily_brief()
        print("所有文件生成完成！")

class FeishuPush:
    def __init__(self, webhook_url):
        """
        初始化飞书推送工具
        :param webhook_url: 飞书机器人webhook URL
        """
        self.webhook_url = webhook_url
    
    def read_file_content(self, file_path):
        """
        读取文件内容
        :param file_path: 文件路径
        :return: 文件内容字符串
        """
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return f.read()
        except Exception as e:
            print(f"读取文件失败: {e}")
            return None
    
    def txt_to_markdown(self, txt_content):
        """
        将txt内容转换为markdown格式
        :param txt_content: txt内容字符串
        :return: markdown格式字符串
        """
        if not txt_content:
            return None
        
        lines = txt_content.split('\n')
        markdown = []
        current_section = None
        
        for line_index, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue
            
            # 处理标题行
            if line.startswith('日期：'):
                # 基本信息部分
                basic_info = []
                basic_info.append(line)
                # 读取接下来的基本信息行
                i = line_index + 1
                while i < len(lines) and not lines[i].strip().startswith('1. '):
                    next_line = lines[i].strip()
                    if next_line:
                        basic_info.append(next_line)
                    i += 1
                # 转换为markdown表格
                markdown.append("## 市场概览")
                for info in basic_info:
                    if ':' in info:
                        key, value = info.split(':', 1)
                        markdown.append(f"**{key}**：{value.strip()}")
                markdown.append("")
                
            # 处理各章节
            elif line.startswith(('1. ', '2. ', '3. ', '4. ', '5. ', '6. ', '7. ', '8. ')):
                # 章节标题
                section_title = line.split('：')[0] if '：' in line else line
                markdown.append(f"## {section_title}")
                current_section = section_title
            
            # 处理列表项（缩进的行）
            elif line.startswith('   '):
                # 移除缩进
                content = line[3:]
                if content:
                    # 处理子项
                    if current_section and '：' in content:
                        # 键值对格式，加粗显示
                        key, value = content.split('：', 1)
                        markdown.append(f"- **{key}**：{value.strip()}")
                    else:
                        # 普通列表项
                        markdown.append(f"- {content}")
            
            # 处理其他内容
            else:
                markdown.append(line)
        
        return '\n'.join(markdown)
    
    def push_text(self, content, title="每日复盘报告"):
        """
        推送文本消息
        :param content: 消息内容
        :param title: 消息标题
        :return: 推送结果
        """
        if not content:
            print("消息内容为空，推送失败")
            return False
        
        # 构建飞书消息格式
        message = {
            "msg_type": "text",
            "content": {
                "text": f"{title}\n\n{content}"
            }
        }
        
        try:
            response = requests.post(
                self.webhook_url,
                headers={"Content-Type": "application/json"},
                data=json.dumps(message)
            )
            
            response_data = response.json()
            if response.status_code == 200 and response_data.get("StatusCode") == 0:
                print("飞书消息推送成功")
                return True
            else:
                print(f"飞书消息推送失败: {response_data}")
                return False
        except Exception as e:
            print(f"飞书消息推送异常: {e}")
            return False
    
    def push_markdown(self, content, title="每日复盘报告"):
        """
        推送Markdown消息
        :param content: Markdown内容
        :param title: 消息标题
        :return: 推送结果
        """
        if not content:
            print("消息内容为空，推送失败")
            return False
        
        # 构建飞书Markdown消息格式
        message = {
            "msg_type": "interactive",
            "card": {
                "config": {
                    "wide_screen_mode": True
                },
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": title
                    },
                    "template": "blue"
                },
                "elements": [
                    {
                        "tag": "markdown",
                        "content": content
                    }
                ]
            }
        }
        
        try:
            response = requests.post(
                self.webhook_url,
                headers={"Content-Type": "application/json"},
                data=json.dumps(message)
            )
            
            response_data = response.json()
            if response.status_code == 200 and response_data.get("StatusCode") == 0:
                print("飞书Markdown消息推送成功")
                return True
            else:
                print(f"飞书Markdown消息推送失败: {response_data}")
                return False
        except Exception as e:
            print(f"飞书Markdown消息推送异常: {e}")
            return False

def get_valid_date_dir(data_lake_dir, mode='clean'):
    """
    获取有效的日期目录
    mode: 'clean' - 清洗模式，使用最新的目录
          'push' - 推送模式，使用有完整数据的最新目录
    """
    # 获取所有日期目录并排序
    dates = [d for d in os.listdir(data_lake_dir) if os.path.isdir(os.path.join(data_lake_dir, d))]
    if not dates:
        print("❌ 没有找到任何日期目录")
        return None
    
    # 按日期降序排序
    dates.sort(reverse=True)
    
    if mode == 'clean':
        # 清洗模式直接返回最新目录
        return dates[0]
    else:
        # 推送模式，寻找有完整数据的最新目录
        for date in dates:
            date_dir = os.path.join(data_lake_dir, date)
            # 检查是否有ai_daily_brief.txt文件
            ai_brief_path = os.path.join(date_dir, "ai_daily_brief.txt")
            if os.path.exists(ai_brief_path):
                # 检查文件是否有内容
                if os.path.getsize(ai_brief_path) > 0:
                    print(f"✅ 找到有效数据目录: {date}")
                    return date
        
        # 如果没有找到有ai_daily_brief.txt的目录，返回最新目录
        print(f"[WARN] 没有找到包含完整数据的目录，使用最新目录: {dates[0]}")
        return dates[0]

# 主函数1：用于运行数据清洗
if __name__ == "__main__":
    import sys
    
    # 解析命令行参数
    target_date = None
    run_mode = 'clean'
    
    i = 1
    while i < len(sys.argv):
        if sys.argv[i] == "push":
            run_mode = 'push'
            i += 1
        elif sys.argv[i] == "--date" and i + 1 < len(sys.argv):
            target_date = sys.argv[i + 1]
            i += 2
        else:
            i += 1
    
    # 运行飞书推送模式
    if run_mode == 'push':
        # 请替换为你的飞书机器人webhook URL
        webhook_url = "https://open.feishu.cn/open-apis/bot/v2/hook/your-webhook-token"
        
        # 获取最新的日期目录作为输出文件夹
        data_lake_dir = r'C:/Users/USER\Desktop\projects\quant\data_lake'
        
        # 如果指定了日期，直接使用该日期
        if target_date:
            latest_date = target_date
            print(f"✅ 使用指定日期: {latest_date}")
        else:
            # 获取有完整数据的最新日期目录
            latest_date = get_valid_date_dir(data_lake_dir, mode='push')
            if not latest_date:
                sys.exit(1)
        
        # 从日期文件夹读取AI提示词文件
        file_path = os.path.join(data_lake_dir, latest_date, "ai_daily_brief.txt")
        feishu = FeishuPush(webhook_url)
        content = feishu.read_file_content(file_path)
        
        if content:
            # 转换为markdown格式
            markdown_content = feishu.txt_to_markdown(content)
            
            if markdown_content:
                # 推送Markdown消息
                feishu.push_markdown(markdown_content)
            else:
                # 转换失败时推送文本消息
                feishu.push_text(content)
        else:
            print(f"❌ 文件内容读取失败或文件为空，无法推送: {file_path}")
    else:
        # 运行数据清洗模式
        data_lake_dir = r'C:/Users/USER\Desktop\projects\quant\data_lake'
        
        # 如果指定了日期，直接使用该日期
        if target_date:
            data_dir = os.path.join(data_lake_dir, target_date)
            if not os.path.exists(data_dir):
                print(f"❌ 指定的日期目录不存在: {data_dir}")
                sys.exit(1)
            cleaner = StockDataCleaner(data_dir)
            cleaner.run()
        else:
            # 默认运行数据清洗模式，获取最新的日期目录
            latest_date = get_valid_date_dir(data_lake_dir, mode='clean')
            if not latest_date:
                sys.exit(1)
            
            data_dir = os.path.join(data_lake_dir, latest_date)
            cleaner = StockDataCleaner(data_dir)
            cleaner.run()
