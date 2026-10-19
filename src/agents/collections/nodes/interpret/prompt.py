"""Instrucciones para extracción; las decisiones comerciales están fuera del LLM."""

from agents.collections.prompts import render_prompt

NODE_PROMPT = render_prompt("interpret")
