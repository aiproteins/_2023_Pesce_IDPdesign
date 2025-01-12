import sys
import os
import argparse
import shutil
import pandas as pd
import random
from analyse import *
from reweight import *
from simulate import *
from mc_moves import MC_move
import time
import shutil

def logger(message):
    logfile = open('LOG', 'a+')
    logfile.write(message)
    logfile.close()

def calc_cmap(pool, w, sigma=1):
    traj = md.load([f'g{x}/traj.dcd' for x in pool],top=f'g{pool[0]}/top.pdb')
    pairs = traj.top.select_pairs('all','all')
    mask = np.abs(pairs[:,0]-pairs[:,1])>1 # exclude bonds
    pairs = pairs[mask]
    d = md.compute_distances(traj,pairs).astype(np.float32)
    d_switch = .5-.5*np.tanh((d-sigma)/.3)
    d_mean = np.average(d_switch,axis=0,weights=w)
    return d_mean

def MSE(x,y):
    return np.sum((x-y)**2)

parser = argparse.ArgumentParser()
parser.add_argument("--target_cmap", type=str)
parser.add_argument("--restart", action='store_true')
parser.add_argument("--fasta", type=str)
parser.add_argument("--L_at_half_acceptance", type=float)
parser.add_argument("--simulated_annealing", action='store_true')
parser.add_argument("--MC_move", type=str)
args = parser.parse_args()

max_pool_size = 5
cmap_target = np.loadtxt(args.target_cmap)
move = args.MC_move

ID = open(args.fasta,'r').readlines()[0].strip().split('>')[1]

if args.restart == True:
    store = pd.read_pickle('evolution.pkl')
    lastgen = store.index.values[-1]
    simdir = [x for x in os.listdir() if os.path.isdir(x) and x.startswith('g')]
    for i in simdir:
        if int(i[1:]) > lastgen:
            shutil.rmtree(i)
    logger('- RESTARTING FROM GENERATION '+str(lastgen)+'\n')
    genstart = lastgen + 1
    c = np.array(store.mc_cp)[-1]
    nc = (store.mc_cp == c).sum()
    pool_ndx = list(store[store.simulate == True].index[-1*max_pool_size:])
    pool_d = []
    pool_mask = []
    for ndx in pool_ndx:
        d, mask = calcD('g'+str(ndx))
        pool_d.append(d)
        pool_mask.append(mask)
    len_pool_i = len(md.load_dcd('g0/traj.dcd', top='g0/top.pdb'))
    emat = np.zeros((len(pool_ndx),len_pool_i*len(pool_ndx)))
    for i_sim, x in enumerate(pool_ndx):
        tmpu = np.array([])
        for i in range(len(pool_d)):
           ene = calcEtot(residues, pool_d[i], parameters.loc[ID], store.fasta[x], pool_mask[i])
           tmpu = np.concatenate((tmpu, ene))
        emat[i_sim] = tmpu
    
else:
    genstart = 1
    c = -1* args.L_at_half_acceptance / np.log(0.5) 
    nc = 0
    logger('- INITIALIZE INPUT SEQUENCE AND SIMULATE\n')
    infasta = open(args.fasta,'r').readlines()
    startseq = list(infasta[1].strip())
    store = pd.DataFrame(columns=['fasta','obs','simulate','mc','mc_cp'])
    store.loc[0] = dict(fasta=startseq,
                        obs=False, 
                        simulate=True,
                        mc=True,
                        mc_cp=c)
    if os.path.isdir('g0'):
        logger('- Start sequence simulation available, skipping simulation\n')
    else:
        simulate(residues, 'g0', store.fasta[0], parameters.loc[ID], 51000000)
    
    d, mask = calcD('g0')
    pool_d = [d]
    pool_mask = [mask]
    pool_ndx = [0]
    emat = calcEtot(residues,d,parameters.loc[ID],store.fasta[0], mask)
    len_pool_i = len(emat)

    cmap = calc_cmap([0], w=np.full(len_pool_i, 1))
    np.savetxt('g0/cmap.dat', cmap)
    store.obs[0] = MSE(cmap,cmap_target)

    store.to_pickle('./evolution.pkl')

logger('- MONTE CARLO VARIABLES:\n')
logger('\tMC Control parameter = '+str(c)+'\n\n')

for n in range(genstart,50000):
    logger('- INITIALIZE GENERATION '+str(n)+'\n')
    store = MC_move(store, c, move)
    
    t0 = time.time()
    mdcheck = [x for x in store[store.simulate].index if store.fasta[x] == store.fasta[n]]
    if len(mdcheck) == 1:
        logger('\tMD simulation available, calculating Obs\n')
        ndx = mdcheck[0]
        cmap = np.loadtxt(f'g{ndx}/cmap.dat')
        store.obs[n] = MSE(cmap,cmap_target)
    else:
        logger('\tAttempting Obs prediction by reweighting\n')
        w, neff = MBAR(emat, residues, parameters.loc[ID], pool_d, pool_mask, len_pool_i, store.fasta[n])  
        logger('\t - Timing MBAR {:.3f}\n'.format(time.time()-t0))
        if (neff < 20000):
            logger('\tNeff = '+str(neff)+'; launching simulation\n')
            simulate(residues, 'g'+str(n), store.fasta[n], parameters.loc[ID], 51000000)
            cmap = calc_cmap([n], w=np.full(len_pool_i, 1))
            np.savetxt(f'g{n}/cmap.dat', cmap)
            store.obs[n] = MSE(cmap,cmap_target)
            store.simulate[n] = True
            logger('\tUpdating pool for MBAR\n\n')
            emat, pool_ndx, pool_d, pool_mask = update_emat(emat, pool_ndx, pool_d, pool_mask, max_pool_size, n, parameters.loc[ID], residues, store.fasta, len_pool_i)
        else:
            logger('\tNeff = '+str(neff)+'; reweighting Obs\n\n')
            cmap = calc_cmap(pool_ndx, w=w)
            store.obs[n] = MSE(cmap,cmap_target)
            logger('\t - Total timing reweighting {:.3f}\n'.format(time.time()-t0))
    
    #MC
    lastmc = store[(store['mc']==True)].index[-1]
    r = random.random()
    logger('\t\tMonte Carlo: random number '+str(r)+'\n')
    L = abs(store.obs[n] - 0) - abs(store.obs[lastmc] - 0)
    logger('\t\tMonte Carlo: cost function '+str(L)+'\n')
    logger('\t\tMonte Carlo: control parameter '+str(c)+'\n')
    L = np.exp(L/-c)
    logger('\t\tMonte Carlo: acceptance ratio '+str(L)+'\n')
    if L > r:
        logger('\t\tMonte Carlo: move accepted\n')
        store.mc[n] = True
    else:
        logger('\t\tMonte Carlo: move refused\n')
    acc_rate = store.mc.sum() / len(store.mc)
    logger('\t\tMonte Carlo: acceptance rate '+str(acc_rate)+'\n\n')

    #SA
    if args.simulated_annealing:
        nc += 1
        if nc >= int(len(store.fasta[n])*2):
            c = 0.99*c
            logger('\t\tSimulated annealing: lowering control parameter to '+str(c)+'\n\n')
            nc = 0
    
    store.to_pickle('./evolution.pkl')
