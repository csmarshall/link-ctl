class LinkCtl < Formula
  include Language::Python::Virtualenv

  desc "CLI and Python library for the Insta360 Link webcam (USB-direct on macOS)"
  homepage "https://github.com/csmarshall/link-ctl"
  url "https://files.pythonhosted.org/packages/ea/bb/efcf2451c56545efc3df5bda42df088d34219848daa1419829173bebe872/link_ctl-2.2.0.tar.gz"
  sha256 "077e244a3bf488602ad28f45cb5231695e339e659782a45ebed6ce7799b06f15"
  license "MIT"

  # Single runtime dependency. (Do NOT add a second `=> :test` line: it
  # doesn't help and is the wrong fix for the "missing test dependencies:
  # python@3.11" CI failure — that's a known Homebrew API-install-mode bug
  # where runtime deps get misclassified as test deps, worked around in
  # ci.yml with HOMEBREW_NO_INSTALL_FROM_API=1. See Homebrew discussion #4150.)
  depends_on "python@3.11"

  resource "websockets" do
    url "https://files.pythonhosted.org/packages/04/24/4b2031d72e840ce4c1ccb255f693b15c334757fc50023e4db9537080b8c4/websockets-16.0.tar.gz"
    sha256 "5f6261a5e56e8d5c42a4497b364ea24d94d9563e8fbd44e78ac40879c60179b5"
  end

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match "usage", shell_output("#{bin}/link-ctl --help 2>&1")
  end
end
