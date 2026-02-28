#!/usr/bin/env python3
"""
TraderJoes EchoEdge — Multi-Model Consensus Bot
Queries Claude, GPT-4o-mini, and GPT-4o in parallel for independent estimates.
"""

import os, sys, json, concurrent.futures
from openai import OpenAI
from anthropic import Anthropic