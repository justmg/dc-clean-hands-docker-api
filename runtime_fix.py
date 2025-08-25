#!/usr/bin/env python3
"""
Runtime fix for Chrome browser path on Heroku
Creates symlink at runtime if it doesn't exist
"""
import os
import subprocess
from pathlib import Path

def fix_chrome_path():
    """Create Chrome symlink at runtime"""
    playwright_chrome = Path("/app/.cache/ms-playwright/chromium-1181/chrome-linux/chrome")
    system_chrome_paths = [
        "/usr/bin/chromium-browser",
        "/snap/bin/chromium", 
        "/usr/bin/google-chrome-stable",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium"
    ]
    
    # Only create symlink if it doesn't exist
    if not playwright_chrome.exists():
        print("🔧 Chrome symlink missing, attempting to create...")
        
        # Find system Chrome
        chrome_exec = None
        for path in system_chrome_paths:
            if Path(path).exists():
                chrome_exec = path
                print(f"✅ Found system Chrome at: {chrome_exec}")
                break
        
        if chrome_exec:
            # Create directory structure
            playwright_chrome.parent.mkdir(parents=True, exist_ok=True)
            
            # Create symlink
            try:
                playwright_chrome.symlink_to(chrome_exec)
                print(f"✅ Created symlink: {playwright_chrome} -> {chrome_exec}")
                return True
            except Exception as e:
                print(f"❌ Failed to create symlink: {e}")
                return False
        else:
            print("❌ No system Chrome found")
            return False
    else:
        print("✅ Chrome symlink already exists")
        return True

if __name__ == "__main__":
    fix_chrome_path()