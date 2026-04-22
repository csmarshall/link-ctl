class LinkCtl < Formula
  include Language::Python::Virtualenv

  desc "CLI and Python library for the Insta360 Link webcam (USB-direct on macOS)"
  homepage "https://github.com/csmarshall/link-ctl"
  url "https://files.pythonhosted.org/packages/2e/00/9b17a1b3fd77421b7d25a8e9cd7090ec38a86b5bcf00826f0c6aca84e073/link_ctl-2.1.1.tar.gz"
  sha256 "6118eb2ddb42a9085b777c0e66a0199d73edabeaad083636b788bf0a5688daf5"
  license "MIT"

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
