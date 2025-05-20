import firebase_admin
from firebase_admin import credentials, firestore, storage
from datetime import datetime
import json
from typing import List, Dict
import feedparser
from dotenv import load_dotenv
import os
from youtube_transcript_api import YouTubeTranscriptApi

#load_dotenv()
admin_sdk_json = "serviceAccount.json"
bucket_name = os.getenv("BUCKET_NAME")

# Firebase初期化
cred = credentials.Certificate(admin_sdk_json)
firebase_admin.initialize_app(cred, {
    'storageBucket': bucket_name  # gs:// は不要
})

# Firestoreクライアント作成
db = firestore.client()

def save_article(title: str, content: str, link: str, thumbnail_url: str, published_at: datetime):
    try:
        article_data = {
            'title': title,
            'content': content,
            'link': link,
            'thumbnailUrl': thumbnail_url,
            'publishedAt': published_at.isoformat(),  # ISO8601文字列
            'createdAt': firestore.SERVER_TIMESTAMP,  # Firestoreのサーバー時刻
        }

        # 'articles'コレクションに新しいドキュメントを追加
        db.collection('articles').add(article_data)

        print('記事を保存しました！')
    except Exception as e:
        print(f'記事保存エラー: {e}')

def save_articles_batch(articles: List[Dict]):
    try:
        batch = db.batch()

        for article in articles:
            # ドキュメント参照を自動IDで作成
            doc_ref = db.collection('articles').document()

            # 書き込むデータの準備
            data = {
                'title': article['title'],
                'content': article['content'],
                'link': article['link'],
                'thumbnailUrl': article['thumbnailUrl'],
                'publishedAt': article['publishedAt'].isoformat(),
                'createdAt': firestore.SERVER_TIMESTAMP,
            }

            # バッチに追加
            batch.set(doc_ref, data)

        # バッチを一括コミット
        batch.commit()
        print(f'{len(articles)} 件の記事を保存しました！')

    except Exception as e:
        print(f'バッチ保存中にエラー: {e}')

def fetch_youtube_videos(channel_id: str):
    rss_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    feed = feedparser.parse(rss_url)

    videos = []
    for entry in feed.entries:
        title = entry.title
        link = entry.link
        yt_videoid = entry.yt_videoid
        published_str = entry.published  # 例: '2023-04-29T01:00:00+00:00'
        published_at = datetime.strptime(published_str, "%Y-%m-%dT%H:%M:%S%z")

        # サムネイルURLの取得（あれば）
        thumbnail_url = entry.media_thumbnail[0]['url'] if 'media_thumbnail' in entry else ''
        transcript_json = get_transcript_as_json(yt_videoid)
        subtitle_url = ""
        if transcript_json:
            subtitle_url = upload_transcript_to_firebase_storage(
                json_content=transcript_json,
                video_id=yt_videoid
            )
    
        videos.append({
            'title': title,
            'link': link,
            'videoId': yt_videoid,
            'thumbnailUrl': thumbnail_url,
            'publishedAt': published_at,
            'subtitleUrl': subtitle_url,
        })
    return videos 

def save_video_to_channel(channel_id: str, channel_name: str, channel_icon_url: str, video_data: Dict):
    try:
        # チャンネル情報も上書き保存しておく（ドキュメント）
        db.collection('channels').document(channel_id).set({
            'channelId': channel_id,
            'channelName': channel_name,
            'channelIconUrl': channel_icon_url,
            'updatedAt': firestore.SERVER_TIMESTAMP,
        }, merge=True)

        # 動画情報（サブコレクション：videos）
        doc_ref = db.collection('channels').document(channel_id).collection('videos').document(video_data['videoId'])
        video_record = {
            'title': video_data['title'],
            'link': video_data['link'],
            'thumbnailUrl': video_data['thumbnailUrl'],
            'subtitleUrl': video_data['subtitleUrl'],
            'publishedAt': video_data['publishedAt'].isoformat(),
            'videoId': video_data['videoId'],
            'createdAt': firestore.SERVER_TIMESTAMP,
            'channelId': channel_id, 
        }

        doc_ref.set(video_record)
        print(f"動画「{video_data['title']}」をチャンネル「{channel_name}」に保存しました。")

    except Exception as e:
        print(f'動画保存中にエラー: {e}')

def save_videos_batch_to_channel(channel_id: str, channel_name: str, channel_icon_url: str, videos: List[Dict]):
    try:
        batch = db.batch()

        # チャンネル情報の更新
        channel_doc_ref = db.collection('channels').document(channel_id)
        batch.set(channel_doc_ref, {
            'channelId': channel_id,
            'channelName': channel_name,
            'channelIconUrl': channel_icon_url,
            'updatedAt': firestore.SERVER_TIMESTAMP,
        }, merge=True)

        for video in videos:
            doc_ref = channel_doc_ref.collection('videos').document(video['videoId'])
            if not doc_ref.get().exists:
                batch.set(doc_ref, {
                    'title': video['title'],
                    'link': video['link'],
                    'thumbnailUrl': video['thumbnailUrl'],
                    'subtitleUrl': video['subtitleUrl'],
                    'publishedAt': video['publishedAt'].isoformat(),
                    'videoId': video['videoId'],
                    'createdAt': firestore.SERVER_TIMESTAMP,
                    'channelId': channel_id,
                })

        batch.commit()
        print(f"{len(videos)} 件の動画をチャンネル「{channel_name}」に保存しました。")

    except Exception as e:
        print(f'動画のバッチ保存中にエラー: {e}')

def load_channels_from_json(file_path='channels.json') -> List[Dict]:
    with open(file_path, 'r', encoding='utf-8') as f:
        return json.load(f)
    
def get_transcript_as_json(video_id: str) -> str | None:
    """
    指定されたYouTube動画IDの英語字幕を取得し、start, end, text を含むJSON形式の文字列に変換する。
    字幕が存在しない場合は None を返す。
    """
    try:
        # 字幕を取得（自動生成含む）
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)

        transcript = None
        for lang_code in ['en', 'en-US', 'en-GB']:
            try:
                candidate = transcript_list.find_transcript([lang_code])
                if candidate.is_generated:
                    continue  # 自動字幕ならスキップ
                transcript = candidate
                break
            except:
                continue

        if transcript is None:
            return None

        raw_data = transcript.fetch().snippets

        # JSON形式に整形（end = start + duration）
        formatted = [
            {
                "start": round(item.start, 3),
                "end": round(item.start + item.duration, 3),
                "text": item.text.replace('\xa0', ' ').strip().replace("’", "'").strip().replace('\n', ' ').strip()
            }
            for item in raw_data
        ]

        return json.dumps(formatted, ensure_ascii=False, indent=2)

    except Exception as e:
        #print(f"⚠️ 字幕取得エラー: {e}")
        return None
        

def upload_transcript_to_firebase_storage(json_content: str, video_id: str) -> str:
    """
    JSON形式の字幕データを Firebase Cloud Storage にアップロードし、公開URLを返す。
    """
    try:
        
        bucket = storage.bucket()
        blob_path = f"transcripts/{video_id}.json"
        blob = bucket.blob(blob_path)

        # JSON文字列をアップロード
        blob.upload_from_string(json_content, content_type='application/json')

        # オブジェクトを公開（バケットが公開設定の場合のみ）
        blob.make_public()

        return blob.public_url
    except Exception as e:
        print(f"⚠️ Cloud Storage アップロードエラー: {e}")
        return ""

# 呼び出し例
if __name__ == "__main__":
    channels = load_channels_from_json()

    for ch in channels:
        print(f"\n▶ チャンネル「{ch['name']}」の動画を取得中...")
        try:
            videos = fetch_youtube_videos(ch['id'])
            print(f"\n▶ チャンネル「{ch['name']}」の動画を取得完了!")

            save_videos_batch_to_channel(
                channel_id=ch['id'],
                channel_name=ch['name'],
                channel_icon_url=ch['channel_icon_url'],
                videos=videos
            )
        except Exception as e:
            print(f"❌ チャンネル「{ch['name']}」の処理中にエラー: {e}")
    
    # rss_feeds = [
    #     "https://feeds.bbci.co.uk/news/world/rss.xml",
    #     "https://www.japantimes.co.jp/feed/news",
    #     "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
    #     # 他のフィードもここに追加可能
    # ]
    
    # for rss_url in rss_feeds:
    #     print(f"\n🌐 RSSフィードから記事取得中: {rss_url}")
    #     articles = fetch_articles_from_rss(rss_url)
    #     save_articles_batch(articles)
    # 使用例
    #channels = load_channels_from_json()
    #for ch in channels:
    #    videos = fetch_youtube_videos(ch['id'])
    #    save_videos_batch_to_channel(ch['id'], ch['name'], ch['channel_icon_url'], videos)
