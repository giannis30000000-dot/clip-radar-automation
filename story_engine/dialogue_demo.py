"""A bounded, authored dialogue demo, not a permanent cast or world."""
from copy import deepcopy
from .providers import DemoStoryProvider, NoNewPremise
from .dialogue import CONCEPT_METRICS, normalize_dialogue, select_concept
from .schema import validate_story


def demo_candidates():
    return [
        {"concept": "An elevator demands a job interview before taking two office workers upstairs, then hires the applicant to carry it.", "category": "situational_comedy", "trope": "job_interview_role_reversal", "scores": {k: 9 for k in CONCEPT_METRICS}},
        {"concept": "A dragon at a bakery discovers its sneezes toast every customer's breakfast and reluctantly becomes the oven.", "category": "fantasy_scifi", "trope": "accidental_useful_talent", "scores": {k: 8 for k in CONCEPT_METRICS}},
        {"concept": "Two bananas say random colors until one disappears without a reason.", "category": "talking_food_objects", "trope": "random_disappearance", "scores": {k: 3 for k in CONCEPT_METRICS}},
    ]


# Speaker, conversational line, visible action. Two turns per scene.
LINES = [
    ("lift", "Your promotion is cancelled.", "The floor display switches to an interview timer."),
    ("mira", "I only pressed seven. Why is the elevator judging me?", "Mira stares at the floor button."),
    ("lift", "Management position. Tell me about a time you lifted others.", "The speaker grille lights up like a suspicious eyebrow."),
    ("mira", "Yesterday I carried Ben through another meeting. Does that count?", "Mira points at Ben's enormous coffee cup."),
    ("ben", "I contributed. I said we should circle back, twice.", "Ben raises two proud fingers."),
    ("lift", "Excellent. You're already qualified to stand still between floors.", "The floor display stops between six and seven."),
    ("mira", "Look, my interview starts in one minute. Just open.", "Mira checks her watch, then the stubborn doors."),
    ("lift", "First question. Where do you see yourself in five years?", "The alarm button becomes an expectant red eye."),
    ("mira", "Honestly? Outside this elevator, preferably before the coffee gets cold.", "Mira folds her arms while Ben guards his coffee."),
    ("ben", "Ambitious. I was going to say right here, near the buttons.", "Ben settles comfortably against the control panel."),
    ("lift", "What's your greatest weakness? Please don't say you're a perfectionist.", "The display flashes a tiny warning triangle."),
    ("mira", "I keep doing everyone's job because it's faster than arguing.", "Mira reaches for the emergency phone and stops herself."),
    ("lift", "Your references?", "The display opens a tiny reference form."),
    ("ben", "Stairs. They say he always brings people down. Escalator refused to comment.", "Ben reads a suspicious reference from his phone."),
    ("mira", "You're interviewing replacements, aren't you? This isn't about my promotion.", "Mira notices a packed suitcase behind the elevator panel."),
    ("lift", "I prefer to call it upward mobility. Sign here.", "The speaker grille points toward the resignation form."),
    ("lift", "Perfect. You're hired. Your first assignment is carrying this team.", "The doors finally open onto the seventh floor."),
    ("mira", "Finally. Come on, Ben. We actually made it on time.", "Mira steps out and gestures for Ben to follow."),
    ("ben", "Wait. Why did the elevator hand you its resignation letter?", "Ben catches a letter sliding from the control panel."),
    ("lift", "I'm taking the stairs. You start Monday. Bring strong shoes.", "A tiny staircase icon waves goodbye on the display."),
]


class DialogueDemoProvider(DemoStoryProvider):
    def generate(self, *, excluded_concepts, template=None):
        selected, candidates = select_concept(demo_candidates(), excluded_concepts)
        if selected["concept"] != demo_candidates()[0]["concept"]:
            raise NoNewPremise("Authored dialogue demo already used; configure production for new casts/concepts")
        characters = [
            {"character_id": "mira", "name": "Mira", "personality": "impatient but resourceful", "speaking_style": "quick dry replies", "visual_description": "office worker, yellow jacket, dark curly hair", "voice_profile_hint": "clear youthful feminine voice"},
            {"character_id": "ben", "name": "Ben", "personality": "cheerfully unhelpful", "speaking_style": "relaxed literal answers", "visual_description": "office worker, blue sweater, large coffee cup", "voice_profile_hint": "warm laid-back masculine voice"},
            {"character_id": "lift", "name": "Lift", "personality": "deadpan exhausted bureaucrat", "speaking_style": "precise interview questions", "visual_description": "brushed steel elevator, expressive floor display", "voice_profile_hint": "low measured dry voice"},
        ]
        for c in characters:
            c["description"] = c["visual_description"]
        dialogue, scenes = [], []
        for index, (speaker, text, action) in enumerate(LINES):
            number = index // 2 + 1
            dialogue.append({"speaker_id": speaker, "text": text, "emotion": "urgent" if speaker == "mira" else "deadpan", "scene_number": number, "action": action, "listeners": [c["character_id"] for c in characters if c["character_id"] != speaker]})
            if index % 2 == 0:
                scenes.append({"scene_number": number, "characters_present": ["mira", "ben", "lift"], "visual_description": action, "visual_prompt": f"Original stylized office comedy. {action} The listeners react silently. No captions or logos.", "action_direction": action, "reaction_direction": "Mira gets less patient while Ben remains delighted.", "camera_direction": ("medium two-shot" if number % 2 else "close reaction shot"), "sound_effect_hint": "soft elevator chime", "background_music_mood": "quiet comic tension", "transition_hint": "cut on the next reply", "payoff_moment": number in (3, 5, 7, 10), "headline": ("THE INTERVIEW", "TEAM PLAYER", "STUCK", "FIVE YEARS", "AMBITION", "WEAKNESS", "REFERENCES", "THE REAL JOB", "HIRED", "THE NEW JOB")[number-1]})
        story = {"story_id": "dialogue-elevator-interview", "title": "The Elevator Interview", "concept": selected["concept"], "content_category": selected["category"], "trope": selected["trope"], "hook": "Your promotion is cancelled.", "characters": characters, "dialogue": dialogue, "scenes": scenes, "ending_type": "standalone", "sequel_possible": False, "art_direction": {"style": "polished stylized cartoon office comedy", "environment": "a small modern elevator"}, "story_beats": {"setup": "Workers need the seventh floor", "goal": "Mira must reach her interview", "conflict": "The elevator insists on interviewing them", "escalation": ["Ben is praised for standing still", "The elevator steals interview time", "Mira accidentally accepts the elevator's job"], "payoff": "The elevator resigns and makes Mira do its lifting"}, "platform_metadata": {p: {"caption": "An original Clip Radar office skit", "publishing_enabled": False} for p in ("instagram", "tiktok", "youtube")}, "generation": {"provider": "authored-dialogue-demo", "candidate_selection": {"selected": deepcopy(selected), "candidates": candidates}, "review_required": True}}
        return validate_story(normalize_dialogue(story))
