#!/usr/bin/env python3
"""
Entry point for Hubble, the agentic coding CLI (hubble package).
Allows running: python code_cli.py [args]   (same as `hubble [args]` after `pip install -e .`)
The previous single-file CLI is still available as: python chat_cli.py
"""
from hubble.main import main

if __name__ == "__main__":
    main()
