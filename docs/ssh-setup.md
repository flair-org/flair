# SSH Setup Guide

Flair uses standard SSH Ed25519 keys and the system `ssh-agent` for verifiable commit signing. This guide walks you through generating a dedicated key, activating `ssh-agent`, and verifying signing readiness.

---

## Quick Workflow

```bash
# 1. Sign in to your Flair account
flair auth login

# 2. Generate and register your signing key
flair auth ssh setup

# 3. Start ssh-agent and load your key (auto-detects shell)
flair auth ssh env

# 4. Verify that the key is loaded in memory
flair auth ssh status
```

---

## Step 1: Generate & Register Key

Prerequisite: Run `flair auth login` first so the backend can associate the public key with your account.

Generate a dedicated Flair signing key (default on Windows: `C:\Users\<username>\.ssh\id_ed25519_flair`, on Linux/macOS: `~/.ssh/id_ed25519_flair`) and register its public key with the backend:

```bash
flair auth ssh setup
```

Example shell output:

```text
Enter SSH key passphrase: 
Repeat for confirmation: 
✓ SSH key setup complete
Private key: C:\Users\<username>\.ssh\id_ed25519_flair
Public key: C:\Users\<username>\.ssh\id_ed25519_flair.pub
Flair principal: ssh:SHA256:ZBVl+TL8IMBSDpbQAcSRYTu6DEtzAd75rDfImNoH7F8
Backend fingerprint: SHA256:ZBVl+TL8IMBSDpbQAcSRYTu6DEtzAd75rDfImNoH7F8
Next: run flair auth ssh env to get ssh-agent activation instructions.
```

Useful setup options include:

```bash
flair auth ssh setup --key-path "C:\Users\<username>\.ssh\id_ed25519_flair"  # Custom key path (Windows)
flair auth ssh setup --key-path ~/.ssh/id_ed25519_flair                      # Custom key path (Linux/macOS)
flair auth ssh setup --no-passphrase                                       # Skip passphrase protection
flair auth ssh setup --overwrite                                           # Overwrite existing key
```

---

## Step 2: Activate SSH Agent and Load Key

Use `flair auth ssh env` to generate the native shell commands that start `ssh-agent` and load your signing key into memory. When invoked without arguments, it automatically detects whether you are running in Command Prompt (CMD), PowerShell, Git Bash, or a Unix shell.

**What `flair auth ssh env` does:**
- **Resolves Key Path**: Targets your Flair private key (default: `C:\Users\<username>\.ssh\id_ed25519_flair` on Windows, `~/.ssh/id_ed25519_flair` on Linux/macOS, or a custom path via `--key-path`).
- **Auto-Detects Environment**: Automatically identifies your active shell or terminal session.
- **Generates Native Commands**: Produces the exact shell commands needed for your platform:
  - *Command Prompt (CMD)*: Configures and starts the Windows `ssh-agent` service with `net start ssh-agent`, then executes `ssh-add "C:\Users\<username>\.ssh\id_ed25519_flair"`.
  - *PowerShell*: Configures and starts the Windows `ssh-agent` service, then executes `ssh-add "C:\Users\<username>\.ssh\id_ed25519_flair"`.
  - *Bash/Zsh*: Spawns the agent with `eval "$(ssh-agent -s)"` and runs `ssh-add '<key_path>'`.
- **Writes or Prints**: If `--output <path>` is provided, writes the snippet to a script for execution; otherwise prints the commands to stdout.
- **Zero Credential Custody**: Flair never handles or stores your passphrase in environment variables; `ssh-add` directly prompts you for passphrase entry.

### Windows (Command Prompt - CMD)

On Windows, the default Flair SSH signing key is located at:

```text
C:\Users\<username>\.ssh\id_ed25519_flair
```

Generate the CMD activation script and call it in your current Command Prompt session:

```cmd
flair auth ssh env --output "C:\Users\<username>\.flair\activate-ssh.cmd"
call "C:\Users\<username>\.flair\activate-ssh.cmd"
```

*(This script automatically sets the `ssh-agent` service to automatic, starts `ssh-agent`, and runs `ssh-add "C:\Users\<username>\.ssh\id_ed25519_flair"`)*.

You will be prompted for your SSH key passphrase.

> [!NOTE]
> **Windows Permission Note**: By default on Windows, the OpenSSH Authentication Agent service (`ssh-agent`) is disabled. If starting the service gives an `Access is denied` or permission error:
> 1. Open Command Prompt as **Administrator** and run once:
>    ```cmd
>    sc config ssh-agent start=auto
>    net start ssh-agent
>    ```
> 2. Then back in your regular Command Prompt session, load the default key directly:
>    ```cmd
>    ssh-add "C:\Users\<username>\.ssh\id_ed25519_flair"
>    ```

### Windows (PowerShell)

On Windows, the default Flair SSH signing key is located at:

```text
C:\Users\<username>\.ssh\id_ed25519_flair
```

Generate the PowerShell activation script and dot-source it in your current PowerShell session:

```powershell
flair auth ssh env --output "$HOME\.flair\activate-ssh.ps1"
. "$HOME\.flair\activate-ssh.ps1"
```

*(This script automatically starts `ssh-agent` and runs `ssh-add "C:\Users\<username>\.ssh\id_ed25519_flair"`)*.

You will be prompted for your SSH key passphrase.

> [!NOTE]
> **Windows Permission Note**: By default on Windows, the OpenSSH Authentication Agent service (`ssh-agent`) is disabled. If starting the service gives an `Access is denied` or permission error:
> 1. Open a PowerShell window as **Administrator** and run once:
>    ```powershell
>    Get-Service ssh-agent | Set-Service -StartupType Automatic
>    Start-Service ssh-agent
>    ```
> 2. Then back in your regular terminal session, load the default key directly:
>    ```powershell
>    ssh-add "C:\Users\<username>\.ssh\id_ed25519_flair"
>    ```
>    *(or using `$HOME`: `ssh-add "$HOME\.ssh\id_ed25519_flair"`)*

### Linux / macOS (Bash / Zsh)

Generate the shell activation script and source it:

```bash
flair auth ssh env --shell bash --output ~/.flair/activate-ssh.sh
source ~/.flair/activate-ssh.sh
```

Alternatively, activate directly in the current shell session without writing a file:

```bash
eval "$(flair auth ssh env --shell bash)"
```

---

## Step 3: Verify Activation Status

Check your SSH setup and agent readiness with `flair auth ssh status`:

```bash
flair auth ssh status
```

**Before activation** (key registered on backend, but not loaded into memory):

```text
                            Flair SSH Status                            
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓     
┃ Item                      ┃ Value                                    ┃     
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩     
│ SSH key path              │ C:\Users\<username>\.ssh\id_ed25519_flair │     
│ SSH public key            │ C:\Users\<username>\.ssh\id_ed25519_flair.pub │     
│ SSH agent loaded keys     │ 0                                        │     
│ Registered Flair SSH keys │ 1                                        │     
│ Session principal         │ google:111456799806545722281             │     
│ Default key exists        │ yes                                      │     
└───────────────────────────┴──────────────────────────────────────────┘     
```

> [!WARNING]
> If `SSH agent loaded keys` is `0`, your key is **not yet active** in the shell session. Commits cannot be signed during `flair push` until the key is loaded into `ssh-agent`.

**After activation** (ready for commit signing):

```text
                            Flair SSH Status                            
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓     
┃ Item                      ┃ Value                                    ┃     
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩     
│ SSH key path              │ C:\Users\<username>\.ssh\id_ed25519_flair │     
│ SSH public key            │ C:\Users\<username>\.ssh\id_ed25519_flair.pub │     
│ SSH agent loaded keys     │ 1                                        │     
│ Registered Flair SSH keys │ 1                                        │     
│ Session principal         │ google:111456799806545722281             │     
│ Default key exists        │ yes                                      │     
└───────────────────────────┴──────────────────────────────────────────┘     
```

When `SSH agent loaded keys` is `1` (or greater), `ssh-agent` has unlocked your key into memory. Any subsequent `flair push` will automatically sign commits using this identity.

For deeper architectural details and dataflows, see the [SSH Integration Reference](working%20notes/ssh-integration.md).
