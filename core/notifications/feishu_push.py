import requests
import json
import time

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
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            # 处理标题行
            if line.startswith('日期：'):
                # 基本信息部分
                basic_info = []
                basic_info.append(line)
                # 读取接下来的基本信息行
                i = lines.index(line) + 1
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

    def push_card(self, card, title="每日复盘报告"):
        """推送飞书原生卡片（支持 table/column_set/hr 等原生组件）。

        飞书 V7.4+ 卡片原生支持 ``tag: "table"`` 组件，渲染效果远优于 markdown
        表格。本方法直接接收已构造好的 card dict，仅负责 HTTP 投递。

        Args:
            card: 飞书卡片 JSON 结构（含 header/elements）。
            title: 日志展示用的标题，不参与卡片渲染（卡片 header 由 card 自身控制）。

        Returns:
            bool: 是否推送成功。
        """
        if not card:
            print("卡片内容为空，推送失败")
            return False

        message = {"msg_type": "interactive", "card": card}
        try:
            response = requests.post(
                self.webhook_url,
                headers={"Content-Type": "application/json"},
                data=json.dumps(message, ensure_ascii=False).encode("utf-8"),
            )
            response_data = response.json()
            if response.status_code == 200 and response_data.get("StatusCode") == 0:
                print(f"飞书卡片消息推送成功: {title}")
                return True
            print(f"飞书卡片消息推送失败: {response_data}")
            return False
        except Exception as e:
            print(f"飞书卡片消息推送异常: {e}")
            return False

def main():
    """
    主函数
    """
    # 请替换为你的飞书机器人webhook URL
    webhook_url = "https://open.feishu.cn/open-apis/bot/v2/hook/your-webhook-token"
    
    # 读取AI提示词文件
    file_path = "ai_daily_brief.txt"
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
        print("文件内容读取失败，无法推送")

if __name__ == "__main__":
    main()
