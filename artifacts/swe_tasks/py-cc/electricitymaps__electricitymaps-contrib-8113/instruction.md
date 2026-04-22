Parser data types in the electricity maps project are represented as raw strings (e.g., "production", "consumption", "exchange") throughout the codebase. This causes several problems:

1. There is no runtime or static validation that a zone configuration exposes a correct parser type. A typo or invalid parser type in a zone config would silently pass without error.
2. Developers must manually write strings like "production" repeatedly when handling parsers, which is error-prone and inconsistent.
3. Updating the set of supported data types is difficult because all string references across the codebase must be found and updated manually, and it's easy to miss some.

To fix this, implement a `ParserDataType` enum that defines all supported parser data types. The enum should include at least the following values: `PRODUCTION`, `CONSUMPTION`, `PRODUCTION_CAPACITY`, `EXCHANGE`, `EXCHANGE_FORECAST`, and any other supported types. Each enum member's value should be the corresponding string (e.g., `PRODUCTION = "production"`).

The `Parsers` model (a Pydantic model used for zone configuration) must have fields that correspond 1:1 to the parser data types defined in the `ParserDataType` enum, excluding exchange-related types (which are handled by a separate `ExchangeParsers` model). There should be a runtime check that validates the `Parsers` model fields match all non-exchange `ParserDataType` enum values exactly.

The `PARSER_DATA_TYPE_TO_DICT` mapping should use `ParserDataType` enum members as keys (e.g., `ParserDataType.PRODUCTION`) instead of raw strings.

The `test_parser` CLI tool should accept a `data-type` argument that is validated against the `ParserDataType` enum. When calling `ParserDataType(data_type)`, it should resolve to the correct enum member. The script should use `PARSER_DATA_TYPE_TO_DICT[ParserDataType(parser_data_type)]` to look up the parser function for a given zone.

Expected behavior: All parser data type references use the `ParserDataType` enum instead of raw strings. The `Parsers` model fields and `ParserDataType` enum values (excluding exchange types) must be kept in sync with a 1:1 correspondence, enforced at runtime. Invalid parser data types should raise a `ValueError` when passed to `ParserDataType()`.