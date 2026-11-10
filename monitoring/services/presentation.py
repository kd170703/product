"""Presentation cleanup; source documents and audit values stay unchanged."""
import re

def clean_public_text(value):
    if not isinstance(value,str):return value
    value=re.sub(r'(?i)\bтребу(?:ет|ют)\s+проверки\b[.!]?','',value)
    value=re.sub(r'[ \t]{2,}',' ',value)
    return value.strip(' ;,')

def clean_public_data(value):
    if isinstance(value,str):return clean_public_text(value)
    if isinstance(value,list):return [clean_public_data(v) for v in value]
    if isinstance(value,dict):return {k:clean_public_data(v) for k,v in value.items()}
    return value
