# Contributing to Sky Radar

Thanks for wanting to improve Sky Radar! Bug fixes, new features, better 3D-printed parts and
documentation are all welcome.

## How changes get in

Nobody pushes directly to `main`. Every change goes through a **pull request (PR)**, and a PR can only
be merged after the maintainer ([@siddhesh1008](https://github.com/siddhesh1008)) has reviewed and approved it.

1. **Fork** this repository. Click **Fork** at the top right of the GitHub page, which gives you your own copy.
2. **Clone your fork** and create a branch for your change:
   ```
   git clone https://github.com/<your-username>/sky-radar.git
   cd sky-radar
   git checkout -b my-change
   ```
3. **Make your change** and test it (see below).
4. **Commit and push** to your fork:
   ```
   git add .
   git commit -m "Short description of the change"
   git push -u origin my-change
   ```
5. **Open a pull request.** GitHub shows a **Compare & pull request** button on your fork. Fill in the
   template: what you changed, why, and how you tested it.
6. **Review.** The maintainer may ask questions or request changes. Push more commits to the same
   branch and the PR updates by itself. Once approved, the maintainer merges it.

For bigger changes, please **open an issue first** to talk about the idea. That saves you work if it
doesn't fit the project.

## Testing your change

- **App:** run `python run.py` (any computer) and check the radar, settings and touch/mouse controls still work.
  To try the Wi-Fi screens without a Raspberry Pi, run it with the environment variable `SKY_RADAR_FAKE_WIFI=1`.
- **On a Raspberry Pi** (if you have one): `python3 run.py`. Say in the PR whether you tested on a Pi.
- **3D parts:** include the updated `.f3d` and exported `.stl` files, and say whether you printed and fitted them.

## Please keep in mind

- **No personal data:** never commit your `location.json`, `prefs.json`, map tiles, Wi-Fi details,
  API keys or passwords. `.gitignore` already excludes the usual files. Double-check with `git status`
  before committing.
- **Keep it simple:** the app is a single Python file using only `pygame-ce` and the standard library.
  Please don't add new dependencies without discussing it first.
- **Free, key-less services only:** anyone should be able to run Sky Radar without signing up for anything.
- By contributing, you agree your contribution is released under the project's [MIT License](LICENSE).
