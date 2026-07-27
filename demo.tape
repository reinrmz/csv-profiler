# vhs tape for the csv-profiler README demo.
#   install: brew install vhs   (or: go install github.com/charmbracelet/vhs@latest)
#   record:  vhs demo.tape
# Writes demo.gif in the repo root. Run from the repo root so the relative
# example path resolves.

Output demo.gif

Set Shell "bash"
Set FontSize 16
Set Width 1000
Set Height 640
Set Padding 20
Set Theme "Catppuccin Mocha"
Set TypingSpeed 60ms

# Let the first frame settle before anything is typed.
Sleep 800ms

Type "python scripts/profile.py examples/messy_sample.csv"
Sleep 600ms
Enter

# Profiling takes well under a second; wait for the JSON to finish painting.
Sleep 2s

# The tail of the output — issues, candidate_keys, outliers — is the payoff.
# Hold on it so it is readable in an inline GitHub GIF.
Sleep 4s
