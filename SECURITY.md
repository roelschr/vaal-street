# Security

Vaal Street is a hobby project with one maintainer. It runs on your own machine, binds only to `127.0.0.1`, and has no accounts and no tokens. The README's first section and its "Screen check (optional)" section say exactly what it reads and what it sends.

## Reporting a problem

Please report a security problem privately, not in a public issue: open the repository's **Security** tab on GitHub and choose **Report a vulnerability**. Say what you found and how to reproduce it. You should get an answer within about a week.

Things worth reporting include: a way for a web page or another machine to reach the local desk, anything that makes the desk, the Windows app or the screen check send data it should not, a way for an imported strategy file to run without the player's consent, and a problem with the published installer.

## Which version is supported

Only the latest release and the `main` branch get fixes.

## The Windows installer

The installer is built by GitHub Actions from a version tag (`.github/workflows/windows-app.yml`) and attached to the release for that tag. It is not signed. Download it only from this repository's Releases page.
