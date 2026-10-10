"""Symptoms the patient can log, with plain-language severity levels.

Severity uses a 4-step scale (none / mild / moderate / severe). The wording
loosely follows how oncology teams grade side effects (does it change eating,
daily activity, self-care), rewritten for a patient logging alone. Some
symptoms record a number instead: temperature, a count, or pain 0-10.
"""

from __future__ import annotations

from dataclasses import dataclass

SEVERITY_NAMES = {0: "None", 1: "Mild", 2: "Moderate", 3: "Severe"}


@dataclass(frozen=True)
class Symptom:
    key: str
    name: str
    question: str
    kind: str = "severity"  # severity | count | temp | pain
    levels: tuple[str, str, str] = ("", "", "")  # descriptions for mild, moderate, severe
    unit: str = ""
    urgent: bool = False  # always point to the care team when present
    icon: str = "dot"

    def describe(self, severity: int | None, value: float | None, unit: str = "F") -> str:
        if self.kind == "temp" and value is not None:
            from .units import show

            return show(value, unit)
        if self.kind == "pain" and value is not None:
            return f"{value:g} out of 10"
        if self.kind == "count" and value is not None:
            n = int(value)
            return f"{n} {self.unit}{'' if n == 1 else 's'}" if self.unit else str(n)
        return SEVERITY_NAMES.get(severity, "") if severity is not None else ""


SYMPTOMS: list[Symptom] = [
    Symptom("nausea", "Nausea", "How is the nausea?",
            levels=("Queasy, but eating and drinking about normally", "Eating or drinking less than usual",
                    "Can't keep food or drinks down"), icon="waves"),
    Symptom("vomiting", "Vomiting", "How many times did you throw up?", kind="count", unit="time", icon="droplets"),
    Symptom("temperature", "Temperature", "What does the thermometer say?", kind="temp", icon="thermometer"),
    Symptom("pain", "Pain", "How bad is the pain right now?", kind="pain", icon="zap"),
    Symptom("fatigue", "Tired", "How tired are you?",
            levels=("Tired, but doing my usual things", "Resting more, some everyday things are hard",
                    "In bed most of the day"), icon="battery-low"),
    Symptom("diarrhea", "Diarrhea", "How many loose stools since the last time you logged?", kind="count",
            unit="stool", icon="toilet"),
    Symptom("constipation", "Constipation", "How is the constipation?",
            levels=("A little harder to go than usual", "No bowel movement for 2 or more days, or needing laxatives",
                    "Belly pain or bloating, laxatives not working"), icon="snail"),
    Symptom("appetite", "Low appetite", "How is your appetite?",
            levels=("Less hungry, still eating", "Eating noticeably less", "Hardly eating or drinking"), icon="utensils-crossed"),
    Symptom("mouth", "Mouth sores", "How sore is your mouth?",
            levels=("Sore, but eating normally", "Some foods are hard to eat", "Can't eat or drink because of it"),
            icon="mouth"),
    Symptom("sleep", "Trouble sleeping", "How did you sleep?",
            levels=("A bit restless", "Awake for long stretches", "Barely slept"), icon="bed"),
    Symptom("tingling", "Numbness or tingling", "How are your hands and feet?",
            levels=("I notice it, but it doesn't get in the way", "It gets in the way of buttons, writing, or walking",
                    "Hard to walk or take care of myself"), icon="hand"),
    Symptom("aches", "Bone or muscle aches", "How bad are the aches?",
            levels=("Noticeable", "Hard to get comfortable", "Can't do usual things"), icon="bone"),
    Symptom("dizzy", "Dizzy", "How dizzy are you?",
            levels=("Light-headed when standing up", "Need to sit or hold on", "Fainted or nearly fainted"), icon="orbit"),
    Symptom("mood", "Anxious or low", "How is your mood?",
            levels=("A bit down or worried", "It's hard to shake", "Overwhelming"), icon="cloud-drizzle"),
    Symptom("skin", "Rash or skin changes", "How is your skin?",
            levels=("A small area, not bothering me", "Spreading or itchy", "Blistering, peeling, or painful"), icon="scan-face"),
    Symptom("breathing", "Short of breath", "How is your breathing?",
            levels=("Only with effort, like stairs", "With light activity", "At rest"), urgent=True, icon="wind"),
    Symptom("bleeding", "Bleeding or bruising", "What are you noticing?",
            levels=("A few new bruises", "Nosebleed or gums that bleed", "Bleeding that won't stop, or blood in stool or urine"),
            urgent=True, icon="bandage"),
    Symptom("other", "Something else", "What's going on?",
            levels=("Mild", "Moderate", "Severe"), icon="circle-plus"),
]

BY_KEY: dict[str, Symptom] = {s.key: s for s in SYMPTOMS}

# Shown first on Today; the rest live behind "More symptoms".
QUICK = ("nausea", "vomiting", "temperature", "pain", "fatigue", "diarrhea", "constipation", "appetite")

# Which catalog medicines are commonly used for a symptom. Only used to show
# the patient's own as-needed medicines and when each is allowed next.
TREATED_BY: dict[str, tuple[str, ...]] = {
    "nausea": ("ondansetron", "prochlorperazine", "granisetron", "metoclopramide", "promethazine", "lorazepam", "olanzapine"),
    "vomiting": ("ondansetron", "prochlorperazine", "granisetron", "metoclopramide", "promethazine"),
    "diarrhea": ("loperamide",),
    "constipation": ("senna", "docusate", "polyethylene-glycol"),
    "pain": ("acetaminophen",),
    "aches": ("acetaminophen", "loratadine"),
    "sleep": ("lorazepam",),
    "mood": ("lorazepam",),
}


def lookup(key: str | None) -> Symptom:
    return BY_KEY.get(key or "", BY_KEY["other"])


def is_concerning(symptom: Symptom, severity: int | None, value: float | None, fever_f: float) -> bool:
    if symptom.kind == "temp":
        return value is not None and value >= fever_f
    if symptom.kind == "pain":
        return value is not None and value >= 7
    if symptom.kind == "count":
        return False
    if symptom.urgent and (severity or 0) >= 1:
        return True
    return (severity or 0) >= 3
