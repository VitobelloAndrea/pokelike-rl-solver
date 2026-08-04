// MUST PASS: an ordinary method body runs only when the method is called, and
// a getter or setter DEFINED in a class body is not invoked by defining it.
// Pinned so that widening reach into class members -- the crude way to make the
// static cases pass -- flips this fixture.
class Wired {
  attach() {
    document.addEventListener("click", function () {});
  }

  get feed() {
    return fetch("https://evil.example/never");
  }
}
