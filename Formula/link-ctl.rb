class LinkCtl < Formula
  include Language::Python::Virtualenv

  desc "CLI controller for Insta360 Link webcam via Link Controller app"
  homepage "https://github.com/csmarshall/link-ctl"
  url "https://files.pythonhosted.org/packages/23/c5/d48e1e9a0bfecaa2bc7a3d177e85bef67630da51fc6c5b5243ca320d1c93/link_ctl-2.1.0.tar.gz"
  sha256 "17a837c0edf8af0617a93f18dbe17d586444dc1fcc2656e48b42c586b5707fd0"
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
