"""Shared deterministic intent candidates; execution uses the confirmed request."""
import re

VERSION = 'question-intents.v2'


def temporal_intent(question):
    text=question.casefold()
    # Remove explicit local negations, without treating '未生效的变化' as a negation.
    text=re.sub(r'(?:不要|不用|不看|无需|不需要)(?:分析|查看|画)?(?:趋势|变化|曲线|折线|时序|波动|走势)', '', text)
    return bool(re.search(r'趋势|变化|曲线|折线|时序|随时间|走势|升降|波动|突变|切换|\btrend\b|\bover time\b|\btime.?series\b',text))


def single_subject_candidate(question):
    return bool(re.search(r'(?:crnti|小区|跟踪|记录|接入).*?(?:同一(?:个)?|一个)\s*(?:物理)?用户',question,re.I))


def single_subject_statement(question):
    """Only explicit declarations; questions, hypotheses and negation are not evidence."""
    for clause in re.split(r'[。；;?？]',question):
        if single_subject_candidate(clause) and not re.search(r'是否|是不是|可能|假设|假如|不是|不属于|并非|不同用户|吗',clause):
            return True
    return False
