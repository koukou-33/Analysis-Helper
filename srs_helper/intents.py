"""Shared deterministic intent candidates; execution uses the confirmed request."""
import re

VERSION = 'question-intents.v1'


def temporal_intent(question):
    text=question.casefold()
    # Remove explicit local negations, without treating '未生效的变化' as a negation.
    text=re.sub(r'(?:不要|不用|不看|无需|不需要)(?:分析|查看|画)?(?:趋势|变化|曲线|折线|时序|波动|走势)', '', text)
    return bool(re.search(r'趋势|变化|曲线|折线|时序|随时间|走势|升降|波动|突变|切换|\btrend\b|\bover time\b|\btime.?series\b',text))


def single_subject_candidate(question):
    return bool(re.search(r'(?:所有|不同|多个|各个)\s*crnti.*?(?:同一|一个)\s*用户',question,re.I))
