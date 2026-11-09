# -*- coding: utf-8 -*-
"""
pyRevit PushButton: Smart Update QMS Tools from GitHub.
Attempts a public download first. If the repository is private or access is denied,
it prompts the user for a GitHub Personal Access Token (PAT), persists it in %AppData%
via script.get_config(), and executes an automated PowerShell synchronization.
"""

import os
import subprocess
from pyrevit import forms, script
from pyrevit.loader import sessionmgr

# --- REPOSITORY & CONFIGURATION KEYS ---
REPO_NAME = "QuadrantMS/pyRevitQMS"
CONFIG_KEY = "github_pat"


# 1. HELPER: EXECUTE BACKGROUND POWERSHELL SYNC
def run_ps_update(token_string):
    ps_template = """
    $ErrorActionPreference = 'Stop'
    try {
        $token = '__TOKEN__'
        $repo = '__REPO__'
        $tempZip = Join-Path $env:TEMP 'pyRevitQMS_latest.zip'
        $extractDir = Join-Path $env:TEMP 'pyRevitQMS_extract'
        $targetDir = Join-Path $env:APPDATA 'pyRevit\\Extensions\\pyRevitQMS.extension'

        $headers = @{
            'User-Agent' = 'pyRevit-Updater'
        }
        
        # Add Authorization header if a token is provided
        if ([string]::IsNullOrWhiteSpace($token) -eq $false) {
            $headers['Authorization'] = "token $token"
        }

        # Enforce TLS 1.2 protocol for secure connection
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri "https://api.github.com/repos/$repo/zipball/main" -Headers $headers -OutFile $tempZip

        if (Test-Path $extractDir) { Remove-Item -Path $extractDir -Recurse -Force }
        Expand-Archive -Path $tempZip -DestinationPath $extractDir -Force

        if (-not (Test-Path $targetDir)) { New-Item -ItemType Directory -Path $targetDir -Force }

        # Locate root directory packaged inside GitHub archive
        $subFolder = Get-ChildItem -Path $extractDir | Where-Object { $_.PSIsContainer } | Select-Object -First 1
        Copy-Item -Path "$($subFolder.FullName)\\*" -Destination $targetDir -Recurse -Force

        Remove-Item -Path $tempZip -Force
        Remove-Item -Path $extractDir -Recurse -Force
        exit 0
    } catch {
        exit 1
    }
    """
    
    ps_command = ps_template.replace("__TOKEN__", token_string).replace("__REPO__", REPO_NAME)
    
    cmd = [
        "powershell.exe",
        "-NoProfile",
        "-WindowStyle", "Hidden",
        "-ExecutionPolicy", "Bypass",
        "-Command", ps_command
    ]
    
    # Returns True if exit code is 0 (success), False otherwise (e.g. 401/404)
    return subprocess.call(cmd) == 0


# 2. USER CONFIRMATION PROMPT
user_confirm = forms.alert(
    "Do you want to check and update QMS Tools to the latest version from GitHub?",
    title="QMS Tools Updater",
    options=["Yes", "No"]
)

if user_confirm != "Yes":
    script.exit()


# 3. RETRIEVE SAVED TOKEN FROM APPDATA CONFIG (IF AVAILABLE)
cfg = script.get_config()
saved_token = cfg.get_option(CONFIG_KEY, "")


# 4. FIRST ATTEMPT: PUBLIC DOWNLOAD OR USE EXISTING SAVED TOKEN
update_success = run_ps_update(saved_token)


# 5. SECOND ATTEMPT: PROMPT FOR TOKEN IF ACCESS FAILED (PRIVATE REPO)
if not update_success:
    new_token = forms.ask_for_string(
        default=saved_token,
        prompt="Repository is Private or access was denied.\n\nPlease enter your GitHub Personal Access Token:",
        title="Authentication Required"
    )
    
    if new_token:
        new_token = new_token.strip()
        retry_success = run_ps_update(new_token)
        
        if retry_success:
            # Persist verified token into %AppData% for future updates
            cfg.set_option(CONFIG_KEY, new_token)
            script.save_config()
            update_success = True
        else:
            forms.alert(
                "Update failed. The provided token is invalid, expired, or lacks read access.", 
                title="Authentication Error", 
                warn_icon=True
            )
            script.exit()
    else:
        # User canceled the token input dialog
        script.exit()


# 6. COMPLETION & AUTOMATIC RIBBON RELOAD
if update_success:
    forms.alert(
        "QMS Tools updated successfully!\n\npyRevit will now automatically reload.",
        title="Update Complete",
        warn_icon=False
    )
    sessionmgr.reload_pyrevit()