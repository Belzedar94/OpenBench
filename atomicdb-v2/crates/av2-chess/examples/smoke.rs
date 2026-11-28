fn main() {
    let where_ = std::env::args().nth(1).unwrap_or_default();
    if where_ == "thread" {
        std::thread::spawn(|| println!("{}", av2_chess::pf::info())).join().unwrap();
    } else {
        println!("{}", av2_chess::pf::info());
    }
}
