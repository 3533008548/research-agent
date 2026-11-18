"""Start the bundled desktop backend."""

from multiprocessing import freeze_support

from api.desktop import main


if __name__ == "__main__":
    freeze_support()
    main()
