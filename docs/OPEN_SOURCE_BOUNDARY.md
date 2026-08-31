# Open-source boundary

HOSPES is open source; a HOSPES deployment's operational records are not.

## Allowed in the public repository

- source code, schemas, migrations, and documentation;
- synthetic fixtures and example configuration;
- public-source datasets whose provenance and redistribution terms are documented;
- opaque examples that cannot be resolved to private systems or people.

## Never commit

- credentials, tokens, private keys, or concrete secret-manager references;
- real email addresses, phone numbers, contact routes, correspondence, or calendar contents;
- private relationship notes, consent records, contracts, deal terms, or unpublished media;
- live databases, exports, transcripts, backups, or provider payloads;
- machine-specific custody paths that identify an operator account.

Runtime data belongs in encrypted, access-controlled storage outside the checkout. Tests use
synthetic identities and reserved example domains. Pull requests must pass the privacy scan and
must not print matched sensitive values into CI logs.

If sensitive data is committed, stop propagation, rotate affected credentials when applicable,
contact the repository owner privately, and coordinate history removal before publishing details.
