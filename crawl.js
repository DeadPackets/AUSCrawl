const Sequelize = require('sequelize').Sequelize;
const chalk = require('chalk');
const puppeteer = require('puppeteer');
const termID = '202010';
const sequelize = new Sequelize(`sqlite:./${termID}.db`, {
    logging: false
});
async function crawl() {
    const CRNS = sequelize.define('crns', {
        id: {
            type: Sequelize.INTEGER,
            autoIncrement: true,
            primaryKey: true
        },
        crn: Sequelize.STRING,
        subject: Sequelize.STRING,
        classTitle: Sequelize.STRING,
        classShortName: Sequelize.STRING,
        classNumber: Sequelize.STRING,
        classSection: Sequelize.INTEGER,
        classType: Sequelize.STRING,
        isLab: Sequelize.BOOLEAN,
        instructor: Sequelize.STRING,
        startTime: Sequelize.TIME,
        endTime: Sequelize.TIME,
        isSunday: Sequelize.BOOLEAN,
        isMonday: Sequelize.BOOLEAN,
        isTuesday: Sequelize.BOOLEAN,
        isWednesday: Sequelize.BOOLEAN,
        isThursday: Sequelize.BOOLEAN,
        levels: Sequelize.STRING,
        attributes: Sequelize.STRING,
        credits: Sequelize.INTEGER,
        classroom: Sequelize.STRING,
        scheduleType: Sequelize.STRING,
        seatsAvailable: Sequelize.BOOLEAN
    });

    const instructors = sequelize.define('instructors', {
        id: {
            type: Sequelize.INTEGER,
            autoIncrement: true,
            primaryKey: true
        },
        name: Sequelize.STRING,
        email: Sequelize.STRING
    });

    const subjects = sequelize.define('subjects', {
        id: {
            type: Sequelize.INTEGER,
            autoIncrement: true,
            primaryKey: true
        },
        shortName: Sequelize.STRING,
        longName: Sequelize.STRING
    });

    const levels = sequelize.define('levels', {
        id: {
            type: Sequelize.INTEGER,
            autoIncrement: true,
            primaryKey: true
        },
        level: Sequelize.STRING
    });

    const attributes = sequelize.define('attributes', {
        id: {
            type: Sequelize.INTEGER,
            autoIncrement: true,
            primaryKey: true
        },
        attribute: Sequelize.STRING
    });

    await sequelize.authenticate();
    await sequelize.sync({
        force: true
    });

    //Now that the database has been setup, time to start crawling
    const browser = await puppeteer.launch({
        args: ['--no-sandbox', '--disable-setuid-sandbox']
    });

    const page = await browser.newPage();
    page.on('error', async (err) => { //For generic errors
        console.log(chalk.red(err));
        await browser.close();
    });

    //Open the first page
    console.log(chalk.blue('Browser and page launched.'))
    await page.goto('https://banner.aus.edu/axp3b21h/owa/bwckschd.p_disp_dyn_sched');
    await page.waitForSelector(`option[VALUE="${termID}`, {
        timeout: 10000
    }).catch(async (err) => {
        await browser.close();
    });
    //Select the semester from the input
    await page.select('select', termID);

    //Click the submit button
    console.log(chalk.blue('Term selected and submitted.'))
    await page.waitForSelector('input[type="submit"]');
    await page.click('input[type="submit"]').catch(async err => {
        await browser.close();
        console.log("INPUT TIMEOUT");
    });

    await page.waitForSelector('select[name="sel_subj"]', {
        timeout: 10000
    }).catch(async err => {
        await browser.close();
    });

    //Time to fetch the subjects
    const subjectFullName = await page.$eval('select[name="sel_subj"]', result => result.innerText.trim().split('\n'));
    const subjectShortName = await page.$$eval('select[name="sel_subj"] option', result => result.map((item) => {
        return item.value
    }));
    
    //Create array for bulk create
    let subjectsArr = [];
    for (let i = 0; i < subjectFullName.length; i++) {
        subjectsArr.push({
            'shortName': subjectShortName[i],
            'longName': subjectFullName[i]
        })
    }

    //Insert subjects into the database
    await subjects.bulkCreate(subjectsArr);
    console.log(chalk.blue(`${subjectFullName.length} subjects inserted into the database.`));

    //Time to crawl CRNs
    await page.select('select[name="sel_subj"]', ...subjectShortName);
    // await page.select('select[name="sel_subj"]', 'COE');
    await page.waitForSelector('input[type="submit"]').catch(async err => {
        await browser.close();
        console.log("INPUT TIMEOUT");
    });
    await page.click('input[type="submit"]');
    await page.waitForSelector('td.dddefault').catch(async err => {
        await browser.close();
    });
    await page.waitForSelector('th a').catch(async err => {
        await browser.close();
    });
    await page.waitForSelector('span.releasetext').catch(async err => {
        await browser.close();
    });
    console.log(chalk.blue('CRN Page loaded.'));

    const totalResults = await page.$$eval('th a', result => {
        let returnedResult = {
            crnInfo: [],
            instructorInfo: []
        };
        for (let i = 0; i < result.length; i++) {
            let crnTitle = result[i].innerText.split(' - ');
            let descriptionElement = result[i].parentElement.parentElement.nextElementSibling;
            let descriptionText = descriptionElement.innerText;
            let classTable = descriptionElement.querySelector('table');

            let info =  {
                'crn': crnTitle[1],
                'subject': crnTitle[2].split(' ')[0],
                'classTitle': crnTitle[0],
                'classShortName': crnTitle[2],
                'classNumber': crnTitle[2].split(' ')[1],
                'classSection': crnTitle[3],
                'classType': classTable.querySelectorAll('td')[6].innerText,
                'isLab': (crnTitle.length === 5 || classTable.querySelectorAll('td')[6].innerText === 'Lab'),
                'instructor': classTable.querySelectorAll('td')[7].innerText.split('(P)')[0],
                'startTime': new Date(`0, ${classTable.querySelectorAll('td')[1].innerText.split(' - ')[0]}`).toString(),
                'endTime': new Date(`0, ${classTable.querySelectorAll('td')[1].innerText.split(' - ')[1]}`).toString(),
                'isSunday': false,
                'isMonday': false,
                'isTuesday': false,
                'isWednesday': false,
                'isThursday': false,
                'levels': descriptionText.match(/(?<=Levels: ).*/g)[0] || null,
                'attributes': null,
                'scheduleType': descriptionText.match(/.+?(?= Schedule)/g)[0] || null,
                'credits': parseInt(descriptionText.match(/.+?(?= Credits)/g)[0]) || null,
                'classroom': classTable.querySelectorAll('td')[4].innerText,
                'seatsAvailable': (classTable.querySelectorAll('td')[3].innerText === 'Y')
            }

            //Slight exception for MTH 103
            if (crnTitle.length === 5 && crnTitle[1].includes('Lab')) {
                info['crn'] = crnTitle[2];
                info['subject'] = crnTitle[3].split(' ')[0];
                info['classNumber'] = crnTitle[3].split(' ')[1];
                info['classTitle'] = `${crnTitle[0]} ${crnTitle[1]}`;
                info['classShortName'] = crnTitle[3];
                info['classSection'] = crnTitle[4];
            } else if (crnTitle[1].includes('Targeted eLipo')) { //Another exception :)
                info['crn'] = crnTitle[2];
                info['subject'] = crnTitle[3].split(' ')[0];
                info['classNumber'] = crnTitle[3].split(' ')[1];
                info['classTitle'] = `${crnTitle[0]} ${crnTitle[1]}`;
                info['classShortName'] = crnTitle[3];
                info['classSection'] = crnTitle[4];
            }

            if (descriptionText.match(/(?<=Attributes: ).*/g)) {
                info['attributes'] = descriptionText.match(/(?<=Attributes: ).*/g)[0]
            }

            let instructorInfo = {
                name: classTable.querySelectorAll('td')[7].innerText.split('(P)')[0].trim(),
                email: (info.instructor === 'TBA') ? 'none' : classTable.querySelector('td a').href.split('mailto:')[1].trim()
            };
            
            let days = classTable.querySelectorAll('td')[2].innerText;
            if (days.includes('U')) {
                info['isSunday'] = true;
            } else if (days.includes('M')) {
                info['isMonday'] = true;
            } else if (days.includes('T')) {
                info['isTuesday'] = true;
            } else if (days.includes('W')) {
                info['isWednesday'] = true;
            } else if (days.includes('R')) {
                info['isThursday'] = true;
            }
            returnedResult.crnInfo.push(info);
            returnedResult.instructorInfo.push(instructorInfo);
        }
        return returnedResult;
    })

    for (let i = 0; i < totalResults.crnInfo.length; i++) {
        //First, we insert the instructor into the database
        let res = await instructors.findOrCreate({
            where: {
                email: totalResults.instructorInfo[i].email
            },
            defaults: totalResults.instructorInfo[i]
        });

        if (res[0]._options.isNewRecord) {
            console.log(chalk.magenta(`Inserting instructor ${totalResults.instructorInfo[i].name} [${totalResults.instructorInfo[i].email}]`))
        }
        

        //Next, we insert the attributes and levels
        if (totalResults.crnInfo[i].attributes) {
            let attributesArr = totalResults.crnInfo[i].attributes.split(', ')
            for (let j = 0; j < attributesArr.length; j++) {
                let res = await attributes.findOrCreate({
                    where: {
                        attribute: attributesArr[j]
                    },
                    defaults: {
                        attribute: attributesArr[j]
                    }
                })

                if (res[0]._options.isNewRecord) {
                    console.log(chalk.yellow(`Inserting attribute ${attributesArr[j]}`))
                }
            }
        }

        if (totalResults.crnInfo[i].levels) {
            let levelsArr = totalResults.crnInfo[i].levels.split(', ')
            for (let j = 0; j < levelsArr.length; j++) {
                let res = await levels.findOrCreate({
                    where: {
                        level: levelsArr[j]
                    },
                    defaults: {
                        level: levelsArr[j]
                    }
                })

                if (res[0]._options.isNewRecord) {
                    console.log(chalk.blue(`Inserting level ${levelsArr[j]}`))
                }
            }
        }

        console.log(chalk.green(`Inserting CRN ${totalResults.crnInfo[i].crn} - ${totalResults.crnInfo[i].classTitle} - ${totalResults.crnInfo[i].classShortName}`))
        await CRNS.create(totalResults.crnInfo[i]);
    }


    await browser.close();

}

crawl().catch((err) => {
    console.log(err);
    console.log(chalk.red('Error! Quitting now...'));
});