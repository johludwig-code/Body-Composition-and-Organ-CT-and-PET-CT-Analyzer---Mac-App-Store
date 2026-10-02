# ADR 0002: Own repository

- Status: accepted
- Date: 2026-10-02

## Context

The owner asked for a fresh start that uses BOCARTA-MOOSE at most as a source,
and chose an own repository for it. BOCARTA-MOOSE is worked on from the
owner's Mac and must not be changed from here.

## Decision

The app lives in `johludwig-code/Body-Composition-and-Organ-CT-and-PET-CT-Analyzer---Mac-App-Store`.
Its first two commits were made in a folder of a branch of BOCARTA-MOOSE and
imported here with `git subtree split`, so the history is kept. Nothing in
this repository refers to BOCARTA-MOOSE's files, and nothing is pushed there.
