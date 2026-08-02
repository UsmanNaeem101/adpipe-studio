# Where to put the image API key

You asked where the key goes. Short answer: **in an environment variable, not in
any file in this project.** Here's exactly how, on your Mac.

---

## 1. Get the key

[platform.openai.com/api-keys](https://platform.openai.com/api-keys) → **Create new
secret key** → copy it. It starts with `sk-`.

You'll also need credit on the account — it's pay-as-you-go and separate from a
ChatGPT Plus subscription. Plus does **not** include API access; they're billed
differently. Add a small amount ($5–10) under Billing; all 14 plates cost roughly
$2.40.

⚠️ You only see the key once. If you lose it, delete it and make a new one.

---

## 2. Put it somewhere

### Option A — just for right now (simplest)

In Terminal, paste this with your key, then run the generator in the *same*
window:

```bash
export OPENAI_API_KEY=sk-your-key-here
```

It lasts until you close that Terminal window. Nothing is written to disk. Good
for a one-off run.

### Option B — permanently (recommended)

Add it to your shell profile so every new Terminal has it:

```bash
echo 'export OPENAI_API_KEY=sk-your-key-here' >> ~/.zshrc
```

Then either open a new Terminal window, or run `source ~/.zshrc` in the current
one. Check it worked:

```bash
echo $OPENAI_API_KEY
```

If that prints your key, you're set.

> `~/.zshrc` is a hidden settings file in your home folder that runs every time
> you open Terminal. It is **not** inside this project and won't get synced to
> OneDrive or committed anywhere.

### What NOT to do

- Don't paste the key into `project.json`, `plates.json`, or any file in this
  folder — this directory syncs to OneDrive.
- Don't paste it into a chat, including with me. (The Anthropic key from earlier
  is in our transcript — delete that one at
  [console.anthropic.com](https://console.anthropic.com) if you haven't.)
- Don't commit it. `.gitignore` covers `.venv/` and `.env`, but the safest key is
  one that was never in a file.

---

## 3. Generate the plates

```bash
cd "/Users/usman/Library/CloudStorage/OneDrive-Personal/Desktop/Ad Templates copy"

# See what it would do and what it costs — spends nothing
.venv/bin/python pipeline/plates.py \
  projects/montisella/output/side_sleepers_night_pain/plates.json --dry-run

# Do it for real, and wire the filenames into concepts.json
.venv/bin/python pipeline/plates.py \
  projects/montisella/output/side_sleepers_night_pain/plates.json --wire

# Re-render the ads with photography behind them
./adpipe render side_sleepers_night_pain
```

Try one first if you want to check the look before spending on all 14:

```bash
.venv/bin/python pipeline/plates.py \
  projects/montisella/output/side_sleepers_night_pain/plates.json --only C02 --wire
./adpipe render side_sleepers_night_pain
```

---

## 4. Check the plates before you ship

**The one thing the compositor cannot catch is lettering inside a plate.** Every
prompt says "no text", but image models still sneak in fake words, signage or
watermarks. Open each generated PNG and look. If one has lettering, regenerate
just that one:

```bash
.venv/bin/python pipeline/plates.py <plates.json> --only C05 --force
```

---

## Using something other than OpenAI

The script talks to any endpoint with the same request shape. Override with env
vars — no code change:

```bash
export IMAGE_API_URL=https://your-provider/v1/images/generations
export IMAGE_MODEL=your-model-name
export IMAGE_SIZE=1024x1536      # portrait, for 4:5 ads
export IMAGE_USD=0.04            # for the cost estimate only
```

`OPENAI_API_KEY` is still the variable it reads for the bearer token, whichever
provider you point it at.

---

## Two different keys

Worth keeping straight, since they do different jobs:

| Key | Variable | What it's for | Needed? |
|---|---|---|---|
| Anthropic | `ANTHROPIC_API_KEY` | The writing stages — reading evidence, writing copy | Not for this batch. The 10 ads are already written and rendered. |
| OpenAI | `OPENAI_API_KEY` | The plates — background and product images | Only if you want photography behind the ads. |

The 10 ads work with neither. Plates are an upgrade.
