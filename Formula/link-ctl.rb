class LinkCtl < Formula
  include Language::Python::Virtualenv

  desc "CLI controller for Insta360 Link webcam via Link Controller app"
  homepage "https://github.com/csmarshall/link-ctl"
  url "https://files.pythonhosted.org/packages/57/e5/62018bd780fa26ca95cc624ce11534e5d6af3a1fb2add612964b51ac033a/link_ctl-1.0.6.tar.gz"
  sha256 "132038841d1a0f9d6000baae453ffe9575dd4a5f640112f230eabb2f0bd37748"
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
