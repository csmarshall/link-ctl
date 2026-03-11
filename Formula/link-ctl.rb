class LinkCtl < Formula
  include Language::Python::Virtualenv

  desc "CLI controller for Insta360 Link webcam via Link Controller app"
  homepage "https://github.com/csmarshall/link-ctl"
  url "https://files.pythonhosted.org/packages/96/7e/b21554c6d259367547f6002612db35ba20fd7f0dd00ade1a662ee4a78aaa/link_ctl-1.0.0.tar.gz"
  sha256 "29c93cf207cfcb3ee56b61ccbfeaa28d742511dd7288282f122b12e4dccd3a92"
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
