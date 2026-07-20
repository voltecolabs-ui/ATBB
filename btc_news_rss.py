#!/usr/bin/env python3
"""BTC News RSS Monitor - мониторинг новостей через RSS"""

import urllib.request
import xml.etree.ElementTree as ET
import json
import os
from datetime import datetime

HERMES_HOME = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes/profiles/trader"))
NOTES_DIR = os.path.join(HERMES_HOME, "workspace", "notes")

# RSS фиды крипто-новостей (международные + русские)
RSS_FEEDS = [
    # Международные
    {"name": "CoinDesk", "url": "https://www.coindesk.com/arc/outboundfeeds/rss/", "category": "news"},
    {"name": "Cointelegraph", "url": "https://cointelegraph.com/rss", "category": "news"},
    {"name": "Bitcoin Magazine", "url": "https://bitcoinmagazine.com/.rss/full/", "category": "bitcoin"},
    {"name": "The Block", "url": "https://www.theblock.co/rss.xml", "category": "research"},
    # Русскоязычные (работают)
    {"name": "ForkLog", "url": "https://forklog.com/feed/", "category": "ru_news"},
]

def fetch_rss(url, timeout=10):
    """Получить RSS фид"""
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.read().decode('utf-8')
    except Exception as e:
        return None

def parse_rss(xml_content, max_age_hours=24):
    """Парсить RSS и извлечь статьи (только свежие)"""
    articles = []
    try:
        root = ET.fromstring(xml_content)
        now = datetime.utcnow()
        
        for item in root.findall('.//item')[:10]:  # Берём до 10
            title = item.find('title')
            link = item.find('link')
            pub_date = item.find('pubDate')
            description = item.find('description')
            
            # Проверка свежести новости
            date_str = pub_date.text if pub_date is not None else ''
            if date_str:
                try:
                    from email.utils import parsedate_to_datetime
                    article_date = parsedate_to_datetime(date_str)
                    age_hours = (now - article_date.replace(tzinfo=None)).total_seconds() / 3600
                    if age_hours > max_age_hours:
                        continue  # Пропустить старую новость
                except:
                    pass  # Если не удалось распарсить дату - берём новость
            
            articles.append({
                'title': title.text if title is not None else 'Без заголовка',
                'link': link.text if link is not None else '',
                'date': date_str,
                'description': (description.text[:200] + '...') if description is not None and description.text else ''
            })
    except Exception as e:
        pass
    return articles[:5]  # Возвращаем максимум 5 свежих

def check_btc_news():
    """Проверить новости о BTC"""
    btc_news = []
    
    for feed in RSS_FEEDS:
        xml_content = fetch_rss(feed['url'])
        if xml_content:
            articles = parse_rss(xml_content)
            for article in articles:
                # Фильтруем статьи о BTC
                title_lower = article['title'].lower()
                desc_lower = article['description'].lower()
                if any(word in title_lower + desc_lower for word in ['bitcoin', 'btc', 'сатоши', 'блокчейн']):
                    article['source'] = feed['name']
                    btc_news.append(article)
    
    return btc_news

def format_news(news_list):
    """Форматировать новости"""
    if not news_list:
        return "Нет новых новостей о BTC"
    
    report = f"📰 Новости BTC | {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
    
    for i, news in enumerate(news_list[:5], 1):
        report += f"{i}. **{news['title']}**\n"
        report += f"   Источник: {news['source']}\n"
        if news['description']:
            report += f"   {news['description']}\n"
        report += "\n"
    
    return report

if __name__ == "__main__":
    print("Проверяю новости BTC...\n")
    news = check_btc_news()
    report = format_news(news)
    print(report)
    
    # Сохраняем
    os.makedirs(NOTES_DIR, exist_ok=True)
    report_file = os.path.join(NOTES_DIR, f"btc_news_{datetime.now().strftime('%Y%m%d_%H%M')}.md")
    with open(report_file, 'w') as f:
        f.write(report)
    print(f"\nСохранено: {report_file}")
