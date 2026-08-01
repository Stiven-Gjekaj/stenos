# Terms and Conditions

These terms govern your use of Stenos, an open-source Discord recording and
transcription bot (the "Software"). By downloading, building, running, or
contributing to the Software, you agree to these terms. If you do not agree, do
not use the Software.

## 1. License

The Software is licensed under the MIT License, included in the
[LICENSE](LICENSE) file. That license is the authoritative statement of your
rights to use, copy, modify, and distribute the Software. If anything here
conflicts with the LICENSE file, the LICENSE file governs.

## 2. No warranty

The Software is provided "as is", without warranty of any kind, express or
implied, including but not limited to the warranties of merchantability,
fitness for a particular purpose, and noninfringement. You use the Software at
your own risk.

Transcription is produced by a statistical model and will contain errors. Do
not rely on a transcript as an accurate record without checking it.

## 3. Limitation of liability

To the maximum extent permitted by law, the authors and copyright holders are
not liable for any claim, damages, or other liability arising from the use of
the Software, whether in an action of contract, tort, or otherwise.

## 4. Recording, consent, and the law

You are responsible for the recordings you make with the Software.

Laws governing the recording of conversations vary by jurisdiction. Some require
the consent of every participant, some require only one party, and some
distinguish private conversations from other settings. Determining what applies
to a given recording, and obtaining any consent required, is your
responsibility and not the authors'.

The Software announces itself by design: it posts a visible message when
recording starts and another when it stops, and Discord shows the bot as
connected to the voice channel. There is no silent recording mode, and requests
to add one will be declined. Modifying the Software to remove those
announcements is permitted by the license, but doing so is your decision and
your liability.

You are also responsible for the recordings and transcripts once they exist.
The Software writes them to disk unencrypted; protecting, retaining, and
deleting them is up to you.

## 5. Acceptable use

Do not use the Software to record anyone unlawfully, to conduct surveillance
without consent where consent is required, or in breach of the Discord Terms of
Service. The Software uses a bot token through Discord's documented API and must
not be modified to automate a user account, which Discord prohibits.

## 6. Third-party components

The Software downloads speech recognition model weights from Hugging Face on
first use, and depends on py-cord, numpy, and either mlx-whisper or
faster-whisper. Those components carry their own licenses and terms, and the
model weights carry the license of their publisher. Your use of them is subject
to those terms, not these.

## 7. Contributions

If you contribute to the Software, you agree that your contributions are
licensed under the same MIT License as the rest of the project. See
[CONTRIBUTING.md](CONTRIBUTING.md) for how to take part.

## 8. Project name

"Stenos" and the `stenos` command name identify this project. You may refer to
the project by name, but please do not use the name in a way that implies
endorsement of, or affiliation with, a modified or unofficial version without
permission.

## 9. Changes to these terms

These terms may change as the project evolves. The version in the default branch
of the repository is the current one. Continued use after a change means you
accept the updated terms.
