if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print(f"\n{PROG}: interrupted", file=sys.stderr)
        sys.exit(130)
