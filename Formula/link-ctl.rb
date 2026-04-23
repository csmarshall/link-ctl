class LinkCtl < Formula
  include Language::Python::Virtualenv

  desc "CLI and Python library for the Insta360 Link webcam (USB-direct on macOS)"
  homepage "https://github.com/csmarshall/link-ctl"
  url "https://files.pythonhosted.org/packages/b4/9f/14017dac0599e9ca33777151acf02902a7c447fdb7ccf19f2b1e28969da1/link_ctl-2.1.2.tar.gz"
  sha256 "d8f87bfd8ba2490eb10f8ef0868d294c86be7f9499aa341bec728f08534fac16"
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
