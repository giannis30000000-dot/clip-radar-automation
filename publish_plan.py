import re


def clean_title(s, limit=90):
    s=re.sub(r"\s+"," ",s).strip()
    return s[:limit].rstrip()


def build_metadata(streamer,title):
    subject=clean_title(title,70)
    hook=f"{streamer}: {subject}"
    tags=[streamer.replace(" ",""),"StreamerClips","Gaming","ClipRadar"]
    hashtags=" ".join("#"+x for x in tags)
    return {
      "youtube":{"title":clean_title(f"{streamer} — {subject} 😳"),"description":f"{hook}\n\n{hashtags}","category":"GAMING"},
      "instagram":{"caption":f"{hook} 👀\n\n{hashtags}"},
      "tiktok":{"caption":f"{hook} 👀 {hashtags}"},
    }
