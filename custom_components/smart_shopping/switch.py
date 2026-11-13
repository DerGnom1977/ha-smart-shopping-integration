"""Plattform switch."""
from .const import DOMAIN
from .framework import platform_setup

async_setup_entry = platform_setup("switch", DOMAIN)
