      * Synthetic healthcare claim with COBOL date fields (example only, no real data)
      * ADMIT-DATE-BIN  : PIC 9(8) COMP   - binary, big-endian, YYYYMMDD (4-digit year)
      * DOB-ZONED        : PIC 9(6)        - zoned decimal (DISPLAY), YYMMDD (2-digit year)
      * DISCHARGE-ZONED  : PIC 9(6)        - zoned decimal (DISPLAY), YYMMDD (2-digit year)
       01  CLAIM-DATE-REC.
           05  CLAIM-ID            PIC X(10).
           05  MEMBER-ID           PIC X(12).
           05  ADMIT-DATE-BIN      PIC 9(8) COMP.
           05  DOB-ZONED           PIC 9(6).
           05  DISCHARGE-ZONED     PIC 9(6).
           05  PROVIDER-NAME       PIC X(20).
