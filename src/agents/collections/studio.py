"""Conversational Studio entry: human messages in, assistant messages out."""

from agents.collections.agent import build_graph

agent = build_graph(studio=True)
