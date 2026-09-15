import re


def clean_title(s, limit=90):
    s=re.sub(r"\s+"," ",s).strip()
    return s[:limit].rstrip()


def _hashtag(value):
    return re.sub(r"[^A-Za-z0-9]", "", value or "")


def build_hook(streamer, title, limit=48):
    """Build a short factual title card; it is not a second subtitle track."""

    broadcaster = clean_title(streamer, 24) or "Streamer"
    subject = clean_title(title, 80) or "gaming moment"
    value = f"{broadcaster}: {subject}"
    if len(value) <= limit:
        return value
    available = max(8, limit - len(broadcaster) - 2)
    words = subject.split()
    shortened = ""
    for word in words:
        proposed = f"{shortened} {word}".strip()
        if len(proposed) > available:
            break
        shortened = proposed
    return f"{broadcaster}: {shortened or subject[:available].rstrip()}"[:limit].rstrip()


def build_metadata(
    streamer,
    title,
    game_name="",
    source_url="",
    transcript_entries=None,
):
    subject=clean_title(title,70) or "Untitled gaming moment"
    broadcaster=clean_title(streamer,40) or "Streamer"
    game=clean_title(game_name,50)
    hook=build_hook(broadcaster, subject)
    tags=[]
    for value in (broadcaster, game, "StreamerClips", "Gaming", "ClipRadar"):
        tag=_hashtag(value)
        if tag and tag.lower() not in {item.lower() for item in tags}:
            tags.append(tag)
    hashtags=" ".join("#"+x for x in tags[:5])
    transcript_text=" ".join(
        clean_title(entry.get("text", ""), 120)
        for entry in (transcript_entries or [])
        if entry.get("text")
    )
    transcript_excerpt=clean_title(transcript_text,160)
    attribution=f"Source: Twitch clip by {broadcaster}"
    description_parts=[hook]
    if game:
        description_parts.append(f"Game: {game}")
    if transcript_excerpt:
        description_parts.append(f"Transcript context: {transcript_excerpt}")
    description_parts.extend([attribution, hashtags])
    return {
      "schema_version":2,
      "hook":hook,
      "streamer":broadcaster,
      "game":game,
      "source_url":source_url,
      "attribution":attribution,
      "hashtags":tags[:5],
      "transcript_excerpt":transcript_excerpt,
      "youtube":{
          "title":clean_title(f"{broadcaster} — {subject} 😳"),
          "description":"\n\n".join(description_parts),
          "category":"GAMING",
          "tags":tags[:5],
      },
      "instagram":{
          "caption":f"{hook} 👀\n\n{attribution}\n\n{hashtags}",
          "type":"REEL",
      },
      "tiktok":{
          "title":clean_title(subject,90),
          "caption":f"{hook} 👀 {attribution} {hashtags}",
      },
    }
