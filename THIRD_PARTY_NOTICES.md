# Third-party dependency licenses

Stage 4 has no third-party runtime Python or JavaScript dependencies. Its Docker
image uses the Python standard library and the bundled HTML, CSS, and JavaScript.

The test-only Python dependencies listed in `stage-4/requirements-dev.txt` are:

| Package | License | Use |
|---|---|---|
| pytest | MIT | Python test runner |
| pytest-playwright | Apache-2.0 | Playwright fixtures for browser tests |

`pytest-playwright` installs Playwright Python and its dependencies. Playwright
Python is Apache-2.0; transitive package licenses can vary by resolved version.
Browser binaries installed with Playwright are separate distributions with their
own notices and license terms. These tools and browser binaries are for local test
use and are not bundled in the Stage 4 runtime image. Review the license metadata
of the exact resolved dependency and browser versions before redistributing them.

The repository does not vendor dependency source or distribute a generated SBOM.
This inventory is based on the direct requirements in `stage-4/requirements-dev.txt`;
recheck transitive dependencies whenever those requirements change.
