# AUSCrawl

![Lines of code](https://img.shields.io/tokei/lines/github/DeadPackets/AUSCrawl)
![GitHub](https://img.shields.io/github/license/DeadPackets/AUSCrawl)
![GitHub package.json version](https://img.shields.io/github/package-json/v/DeadPackets/AUSCrawl)
![GitHub issues](https://img.shields.io/github/issues/DeadPackets/AUSCrawl)
[![Maintenance](https://img.shields.io/badge/Maintained%3F-yes-green.svg)](https://GitHub.com/Naereen/StrapDown.js/graphs/commit-activity)
[![Open Source Love](https://badges.frapsoft.com/os/v1/open-source.png?v=103)](https://github.com/ellerbrock/open-source-badges/)


**AUSCrawl** is a web scraper and crawler that scrapes [AUS Banner](https://banner.aus.edu/) for data on every single course, instructor, level, and attribute for **every semester in AUS since 2005** and saves it in an SQLite database to be queried.

## Why create this project?

I created this project as a way to practice using a headless browser to scrape mass data while also learning asynchronous code, using the Sequelize ORM and optimizing my code in general. Additionally, I think the dataset this project produces can allow many others to practice data science or build applications that make use of this data.

## Prerequisites

To run this project, you will need NodeJS. I recommend using any version after v14.

## How to get started

1. Download the repository: `git clone https://github.com/DeadPackets/AUSCrawl`
2. Enter the project and download required libraries: `cd AUSCrawl && npm install`
3. Now, simply run the project: `node crawl.js`
    1. Additionally, if you want verbose output, run the following: `VERBOSE=true node crawl.js`

## Libraries used in the project

- **Chalk** is used for coloring the console output
- **Sequelize** is the database ORM used to save the crawled data into SQLite
- **Puppeteer** is the headless browser library used to browse and crawl the data from banner.

## How does it work?

I am planning on writing a blog post **soon**.

## Contribution

Sure! Simply fork the project, add your feature/fix and make a pull request. I will review them ASAP.
